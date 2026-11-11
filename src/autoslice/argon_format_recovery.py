"""One explicitly authorised, durable Argon format-repair successor.

Argon already keeps the exact provider response and makes a contract failure
terminal.  This module adds the deliberately narrow missing edge: a complete
provider response that failed only the local representation contract may have
one separately ledgered successor, when an exact-original authorization is
present.  The original task is never edited and its request/response/ledger
remain the predecessor of the successor.

The provider and route are injected by :mod:`argon_escalation_runtime`; this
module does not load credentials or create a transport.  A successor is only
useful when the normal Argon parser accepts it.  In particular, no malformed
field is coerced locally and transport unknowns are not format-retry consent.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import time
from pathlib import Path
from typing import Callable, Mapping, cast

from . import argon_escalation_storage as store
from .argon_escalation_contract import (
    AudioSpec,
    KeyPolicy,
    MODEL,
    canonical,
    require,
    sha,
    strict_json,
    valid_sha,
)
from .argon_escalation_witness import ArgonEscalationWitness, Provider
from .cpa_gemini_client import JsonMap, JsonObject, JsonValue
from .content_bound_request_ledger import ContentBoundRequestLedger


AUTHORIZATION_SCHEMA = "argon-format-recovery-authorization.v1"
PREDECESSOR_SCHEMA = "argon-format-recovery-predecessor.v1"
INTENT_SCHEMA = "argon-format-recovery-intent.v1"
RECEIPT_SCHEMA = "argon-format-recovery-receipt.v1"
RECOVERY_SCHEMA = "argon-format-recovery.v1"
_NONTERMINAL_SUCCESSOR_STATUSES = frozenset(
    {"PENDING", "WAIT_TIMEOUT", "REMOTE_STATE_UNKNOWN", "READBACK_UNKNOWN", "SUBMIT_UNKNOWN"}
)

# These are parser/representation failures after a complete HTTP response.
# Clock, binding, transport, remote-state and valid hearing statuses are
# intentionally absent.  The local parser remains the only source of truth.
FORMAT_FAILURE_REASONS = frozenset(
    {
        "INVALID_JSON",
        "RESPONSE_CANDIDATES_INCOMPLETE",
        "RESPONSE_PART_INVALID",
        "RESPONSE_TEXT_MISSING",
        "HEARING_SCHEMA_INVALID",
        "HEARING_STATUS_INVALID",
        "HEARING_TEXT_INVALID",
        "HEARING_ROLE_INVALID",
        "HEARING_REASON_INVALID",
        "HEARING_UNCERTAINTIES_INVALID",
        "HEARING_STATUS_CONTRADICTION",
    }
)


def _is_sha(value: object) -> bool:
    return valid_sha(value)


def _object(value: JsonValue, reason: str = "FORMAT_JSON_OBJECT_REQUIRED") -> JsonObject:
    if isinstance(value, dict):
        return value
    require(False, reason)
    raise AssertionError(reason)


def _array(value: JsonValue, reason: str = "FORMAT_JSON_ARRAY_REQUIRED") -> list[JsonValue]:
    if isinstance(value, list):
        return value
    require(False, reason)
    raise AssertionError(reason)


def _text(value: JsonValue, reason: str = "FORMAT_JSON_STRING_REQUIRED") -> str:
    if isinstance(value, str):
        return value
    require(False, reason)
    raise AssertionError(reason)


def _optional_text(value: JsonValue, reason: str = "FORMAT_JSON_STRING_REQUIRED") -> str | None:
    if value is None:
        return None
    return _text(value, reason)


def _integer(value: JsonValue, reason: str = "FORMAT_JSON_INTEGER_REQUIRED") -> int:
    if type(value) is int:
        return value
    require(False, reason)
    raise AssertionError(reason)


def _safe_receipt(value: object) -> bool:
    """Keep authorization receipts metadata-only before persisting them."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                return False
            lowered = key.casefold()
            if any(token in lowered for token in ("secret", "token", "api_key", "password")):
                return False
            if not _safe_receipt(item):
                return False
        return True
    if isinstance(value, (list, tuple)):
        return all(_safe_receipt(item) for item in value)
    if isinstance(value, str):
        return not value.lstrip().casefold().startswith(("http://", "https://", "bearer "))
    return value is None or isinstance(value, (bool, int, float))


def make_authorization(
    original: JsonMap,
    *,
    receipt: JsonMap,
    durable_scope: str = "argon-format-recovery",
) -> JsonObject:
    """Build the exact-original authorization consumed by ``recover``.

    ``original`` is the hash-only predecessor proof returned by
    ``predecessor_proof``.  The caller supplies the human/administrative
    receipt; it is persisted as metadata but never interpreted as a new prompt
    or a replacement content decision.
    """

    require(isinstance(original, Mapping) and bool(original), "FORMAT_AUTH_ORIGINAL_INVALID")
    require(isinstance(receipt, Mapping) and bool(receipt), "FORMAT_AUTH_RECEIPT_INVALID")
    require(_safe_receipt(receipt), "FORMAT_AUTH_RECEIPT_UNSAFE")
    require(isinstance(durable_scope, str) and bool(durable_scope.strip()), "FORMAT_AUTH_SCOPE_INVALID")
    return {
        "schema_version": AUTHORIZATION_SCHEMA,
        "authorized": True,
        "durable_scope": durable_scope,
        "repair_ordinal": 1,
        "max_additional_dispatches": 1,
        "original": deepcopy(dict(original)),
        "receipt": deepcopy(dict(receipt)),
    }


def _inner_prompt_sha(inner: bytes) -> str:
    request = cast(JsonObject, strict_json(inner))
    contents = _array(request.get("contents"), "FORMAT_PROMPT_SHAPE_INVALID")
    require(len(contents) == 1, "FORMAT_PROMPT_SHAPE_INVALID")
    first = _object(contents[0], "FORMAT_PROMPT_SHAPE_INVALID")
    parts = _array(first.get("parts"), "FORMAT_PROMPT_SHAPE_INVALID")
    texts = [
        _text(_object(part, "FORMAT_PROMPT_SHAPE_INVALID").get("text"),
              "FORMAT_PROMPT_SHAPE_INVALID")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    require(len(texts) == 1, "FORMAT_PROMPT_SHAPE_INVALID")
    return sha(texts[0].encode("utf-8"))


def _schema_sha() -> str:
    from .response_schemas import hearing_schema

    schema_factory = cast(Callable[[], JsonObject], hearing_schema)
    return sha(canonical(schema_factory()))


def _original_response_complete(task: Path, response_sha256: str) -> bool:
    calls = task / "calls"
    if not calls.is_dir() or calls.is_symlink():
        return False
    found = False
    for path in sorted(calls.glob("*.outcome.json")):
        try:
            value = cast(JsonObject, store.get_json(path))
        except Exception:
            continue
        if value.get("response_sha256") != response_sha256:
            continue
        found = found or (
            value.get("http_status") == 200 and value.get("response_complete") is True
        )
    return found


def _wire_schema_sha(task: Path, request_sha256: str) -> str:
    """Read the immutable POST start receipt for the structured wire contract."""

    found: list[JsonObject] = []
    for path in sorted((task / "calls").glob("*.start.json")):
        try:
            value = cast(JsonObject, store.get_json(path))
        except Exception:
            continue
        if value.get("method") != "POST" or value.get("request_sha256") != request_sha256:
            continue
        structured = value.get("structured_output")
        if isinstance(structured, dict):
            found.append(structured)
    require(len(found) == 1, "FORMAT_WIRE_SCHEMA_RECEIPT_INVALID")
    structured = found[0]
    require(
        structured.get("json_mode") is True
        and valid_sha(structured.get("schema_sha256"))
        and structured.get("schema_sha256") == _schema_sha(),
        "FORMAT_WIRE_SCHEMA_RECEIPT_INVALID",
    )
    return _text(structured["schema_sha256"], "FORMAT_WIRE_SCHEMA_RECEIPT_INVALID")


def predecessor_proof(
    engine: ArgonEscalationWitness,
    original_result: JsonMap,
    *,
    expected: AudioSpec,
) -> JsonObject:
    """Read and validate a complete, format-only failed predecessor."""

    require(isinstance(original_result, Mapping), "FORMAT_ORIGINAL_RESULT_INVALID")
    require(original_result.get("status") == "RESPONSE_REJECTED", "FORMAT_ORIGINAL_NOT_REJECTED")
    reason = _text(original_result.get("reason"), "FORMAT_ORIGINAL_REASON_NOT_REPAIRABLE")
    require(reason in FORMAT_FAILURE_REASONS, "FORMAT_ORIGINAL_REASON_NOT_REPAIRABLE")
    task_id = _text(original_result.get("task_id"), "FORMAT_ORIGINAL_TASK_INVALID")
    mode = _text(original_result.get("mode"), "FORMAT_ORIGINAL_MODE_INVALID")
    require(valid_sha(task_id), "FORMAT_ORIGINAL_TASK_INVALID")
    require(mode in {"batch", "realtime"}, "FORMAT_ORIGINAL_MODE_INVALID")
    task, manifest_raw, inner = engine._load(task_id, expected)
    manifest = cast(JsonObject, manifest_raw)
    ledger = engine._ledger(task, manifest, mode, engine._body(task_id, inner, mode))
    snapshot = cast(JsonObject, engine._read_snapshot(ledger))
    terminal = _object(_array(snapshot["events"])[-1], "FORMAT_ORIGINAL_LEDGER_INVALID")
    require(snapshot["state"] == "FAILED", "FORMAT_ORIGINAL_LEDGER_NOT_FAILED")
    require(terminal["event"] == "ARGON_RESULT_RETAINED", "FORMAT_ORIGINAL_RESULT_NOT_RETAINED")
    response_sha256 = _text(original_result.get("response_sha256"), "FORMAT_ORIGINAL_HASH_INVALID")
    result_sha256 = _text(original_result.get("result_sha256"), "FORMAT_ORIGINAL_HASH_INVALID")
    require(_is_sha(response_sha256) and _is_sha(result_sha256), "FORMAT_ORIGINAL_HASH_INVALID")
    metadata = _object(terminal["metadata"], "FORMAT_ORIGINAL_LEDGER_INVALID")
    require(
        metadata.get("response_sha256") == response_sha256
        and metadata.get("result_sha256") == result_sha256,
        "FORMAT_ORIGINAL_TERMINAL_DRIFT",
    )
    result_raw = store.get_blob(task, result_sha256)
    sealed = cast(JsonObject, strict_json(result_raw))
    supplied = {
        key: value
        for key, value in original_result.items()
        if key not in {"result_sha256", "accounting", "format_recovery"}
    }
    require(canonical(supplied) == canonical(sealed), "FORMAT_ORIGINAL_RESULT_DRIFT")
    store.get_blob(task, response_sha256)
    require(_original_response_complete(task, response_sha256), "FORMAT_ORIGINAL_RESPONSE_INCOMPLETE")
    request_sha256 = sha(engine._body(task_id, inner, mode))
    binding = _object(manifest["binding"], "FORMAT_ORIGINAL_BINDING_INVALID")
    binding_sha256 = sha(canonical(binding))
    require(original_result.get("request_sha256") == request_sha256, "FORMAT_ORIGINAL_REQUEST_DRIFT")
    require(original_result.get("inner_request_sha256") == sha(inner), "FORMAT_ORIGINAL_INNER_DRIFT")
    dispatch = cast(JsonObject, engine._dispatch_metadata(snapshot))
    route = _object(dispatch.get("route"), "FORMAT_ORIGINAL_ROUTE_MISSING")
    wire_schema_sha256 = _wire_schema_sha(task, request_sha256)
    return {
        "schema_version": PREDECESSOR_SCHEMA,
        "task_id": task_id,
        "engine_root": str(engine.root),
        "mode": mode,
        "requested_model": MODEL,
        "binding_sha256": binding_sha256,
        "request_sha256": request_sha256,
        "inner_request_sha256": sha(inner),
        "prompt_sha256": _inner_prompt_sha(inner),
        "schema_sha256": _schema_sha(),
        "wire_schema_sha256": wire_schema_sha256,
        "input_audio_sha256": _text(binding["input_audio_sha256"], "FORMAT_ORIGINAL_BINDING_INVALID"),
        "response_sha256": response_sha256,
        "result_sha256": result_sha256,
        "ledger_sha256": sha(store.read(ledger.path)),
        "ledger_state": snapshot["state"],
        "failure_reason": reason,
        "route": deepcopy(dict(route)),
    }


def validate_authorization(authorization: JsonMap, predecessor: JsonMap) -> JsonObject:
    require(isinstance(authorization, Mapping), "FORMAT_AUTH_INVALID")
    require(
        set(authorization)
        == {
            "schema_version",
            "authorized",
            "durable_scope",
            "repair_ordinal",
            "max_additional_dispatches",
            "original",
            "receipt",
        },
        "FORMAT_AUTH_FIELDS_INVALID",
    )
    require(authorization.get("schema_version") == AUTHORIZATION_SCHEMA, "FORMAT_AUTH_SCHEMA_INVALID")
    require(authorization.get("authorized") is True, "FORMAT_AUTH_NOT_GRANTED")
    require(authorization.get("repair_ordinal") == 1, "FORMAT_AUTH_ORDINAL_INVALID")
    require(authorization.get("max_additional_dispatches") == 1, "FORMAT_AUTH_LIMIT_INVALID")
    scope = authorization.get("durable_scope")
    require(isinstance(scope, str) and bool(scope.strip()), "FORMAT_AUTH_SCOPE_INVALID")
    require(authorization.get("original") == dict(predecessor), "FORMAT_AUTH_ORIGINAL_MISMATCH")
    receipt = authorization.get("receipt")
    require(isinstance(receipt, Mapping) and bool(receipt), "FORMAT_AUTH_RECEIPT_INVALID")
    require(_safe_receipt(receipt), "FORMAT_AUTH_RECEIPT_UNSAFE")
    return dict(authorization)


def _copy_immutable(source: Path, target: Path) -> None:
    require(source.is_file() and not source.is_symlink(), "FORMAT_COPY_SOURCE_MISSING")
    store.immutable(target, store.read(source))


def _copy_runtime_scope(original_root: Path, successor_root: Path, route: JsonMap) -> None:
    store.directory(successor_root)
    names = ["routing-policy.json", "routing-policy-seal.json", "free-chain-identity.json"]
    ordinal = route.get("accepted_key_ordinal")
    if type(ordinal) is int:
        names.append("credential-" + str(ordinal) + ".json")
    for name in names:
        source = original_root / name
        if source.exists():
            _copy_immutable(source, successor_root / name)


def _copy_capability_artifacts(original_task: Path, successor_task: Path) -> None:
    for mode in ("batch", "realtime"):
        for suffix in (".json", ".intent.json"):
            source = original_task / ("capability-api-" + mode + suffix)
            if source.exists():
                _copy_immutable(source, successor_task / source.name)
                if suffix == ".json":
                    pointer = cast(JsonObject, store.get_json(source))
                    receipt_value = pointer.get("receipt_sha256")
                    if isinstance(receipt_value, str) and valid_sha(receipt_value):
                        source_blob = original_task / "blobs" / (receipt_value + ".bin")
                        _copy_immutable(source_blob, successor_task / "blobs" / source_blob.name)
                        receipt = cast(JsonObject, store.get_json(
                            original_task / "blobs" / (receipt_value + ".bin")
                        ))
                        for key in ("raw_sha256", "stderr_sha256"):
                            digest = receipt.get(key)
                            if isinstance(digest, str) and valid_sha(digest):
                                source_blob = original_task / "blobs" / (digest + ".bin")
                                _copy_immutable(source_blob, successor_task / "blobs" / source_blob.name)


class _FixedRoutePolicy:
    def __init__(self, route: JsonMap, capability_receipt: str | None, task_id: str, mode: str):
        self.route: JsonObject = deepcopy(dict(route))
        self.capability: str | None = capability_receipt
        self.task_id = task_id
        self.mode = mode

    def select(self, item_key: str) -> JsonObject:
        require(item_key == self.task_id, "FORMAT_ROUTE_ITEM_DRIFT")
        return deepcopy(self.route)

    def capability_receipt(self, mode: str, route: JsonMap, request_sha256: str) -> str | None:
        require(mode == self.mode and canonical(dict(route)) == canonical(self.route),
                "FORMAT_ROUTE_DRIFT")
        return self.capability


def _wrapper_state_after_result(
    wrapper: ContentBoundRequestLedger,
    child_engine: ArgonEscalationWitness,
    child_task: Path,
    child_manifest: JsonObject,
    child_inner: bytes,
    mode: str,
    result: JsonMap,
) -> JsonObject:
    wrapper_snapshot = cast(JsonObject, wrapper.snapshot())
    state = _text(wrapper_snapshot["state"])
    if state not in {"SUBMIT_DISPATCHED", "SUBMIT_AMBIGUOUS", "PROCESSING", "QUEUED", "RESULT_AVAILABLE"}:
        return wrapper_snapshot
    child_ledger = child_engine._ledger(child_task, child_manifest,
                                        mode, child_engine._body(child_task.name, child_inner, mode))
    child_snapshot = cast(JsonObject, child_engine._read_snapshot(child_ledger))
    status = result.get("status")
    reason = _optional_text(result.get("reason"), "FORMAT_SUCCESSOR_REASON_INVALID")
    if status == "SUBMIT_UNKNOWN":
        if state == "SUBMIT_DISPATCHED":
            return wrapper.transition("SUBMIT_AMBIGUOUS", event="FORMAT_RECOVERY_UNKNOWN",
                                      reason_code=reason, metadata={
                                          "successor_ledger_sha256": sha(store.read(child_ledger.path))})
        return cast(JsonObject, wrapper.snapshot())
    if status in {"PENDING", "WAIT_TIMEOUT", "REMOTE_STATE_UNKNOWN", "READBACK_UNKNOWN"}:
        next_state = "PROCESSING" if child_snapshot["state"] == "PROCESSING" else "QUEUED"
        if next_state == state:
            return cast(JsonObject, wrapper.snapshot())
        if next_state in {"PROCESSING", "QUEUED"}:
            return wrapper.transition(next_state, event="FORMAT_RECOVERY_PROCESSING",
                                      reason_code=reason, metadata={
                                          "successor_ledger_sha256": sha(store.read(child_ledger.path))})
        return cast(JsonObject, wrapper.snapshot())
    if status in {"OBSERVED", "UNCERTAIN", "NO_SPEECH", "ERROR", "RESPONSE_REJECTED", "FAILED"}:
        if state != "RESULT_AVAILABLE":
            wrapper.transition("RESULT_AVAILABLE", event="FORMAT_RECOVERY_RESULT_AVAILABLE",
                               reason_code=reason, metadata={
                                   "successor_ledger_sha256": sha(store.read(child_ledger.path)),
                                   "response_sha256": result.get("response_sha256"),
                               })
        final = "FAILED" if status in {"RESPONSE_REJECTED", "FAILED"} else "COMPLETED"
        if cast(JsonObject, wrapper.snapshot())["state"] == "RESULT_AVAILABLE":
            return wrapper.transition(final, event="FORMAT_RECOVERY_RESULT_RETAINED",
                                      reason_code=reason, metadata={
                                          "result_sha256": result.get("result_sha256"),
                                          "response_sha256": result.get("response_sha256"),
                                      })
        return cast(JsonObject, wrapper.snapshot())
    return wrapper.transition("FAILED", event="FORMAT_RECOVERY_REJECTED",
                              reason_code="FORMAT_SUCCESSOR_STATUS_INVALID", metadata={})


def recover(
    *,
    parent_root: Path,
    original_engine: ArgonEscalationWitness,
    original_result: JsonMap,
    expected: AudioSpec,
    authorization: JsonMap,
    provider: Provider,
    request_timeout_s: float = 60,
    max_batch_wait_s: float = 86400,
    max_recovery_pages: int = 1,
    now: Callable[[], float] = time.time,
) -> JsonObject:
    """Run or cold-resume the one authorized format successor."""

    predecessor = predecessor_proof(original_engine, original_result, expected=expected)
    auth = validate_authorization(authorization, predecessor)
    parent_root = Path(parent_root).absolute()
    recovery_root = parent_root / "format-recovery"
    successor_engine_root = recovery_root / "attempt-1" / "engine"
    store.directory(recovery_root)
    auth_raw = canonical(auth)
    proof_raw = canonical(predecessor)
    store.immutable(recovery_root / "authorization.json", auth_raw)
    store.immutable(recovery_root / "predecessor-proof.json", proof_raw)
    _copy_runtime_scope(parent_root, recovery_root, _object(predecessor["route"]))

    successor_engine = ArgonEscalationWitness(successor_engine_root)
    handle = cast(JsonObject, successor_engine.prepare(expected))
    handle_task_id = _text(handle["task_id"], "FORMAT_SUCCESSOR_TASK_DRIFT")
    require(handle_task_id == predecessor["task_id"], "FORMAT_SUCCESSOR_TASK_DRIFT")
    child_task, child_manifest_raw, child_inner = successor_engine._load(handle_task_id, expected)
    child_manifest = cast(JsonObject, child_manifest_raw)
    require(sha(child_inner) == predecessor["inner_request_sha256"], "FORMAT_SUCCESSOR_PROMPT_DRIFT")
    # The predecessor task path is the actual engine root plus task id.  Do not
    # discover a similarly named task elsewhere in the cache.
    original_task = Path(_text(predecessor["engine_root"])) / _text(predecessor["task_id"])
    require(original_task.is_dir() and not original_task.is_symlink(),
            "FORMAT_ORIGINAL_TASK_MISSING")
    _copy_capability_artifacts(original_task, child_task)

    wrapper_binding = {
        "provider": "gemini_api",
        "requested_model": MODEL,
        "mode": _text(predecessor["mode"]),
        "task_id": _text(predecessor["task_id"]),
        "request_sha256": _text(predecessor["request_sha256"]),
        "input_audio_sha256": _text(predecessor["input_audio_sha256"]),
        "format_recovery": {
            "schema_version": RECOVERY_SCHEMA,
            "repair_ordinal": 1,
            "predecessor_proof_sha256": sha(proof_raw),
            "authorization_sha256": sha(auth_raw),
            "successor_task_id": child_task.name,
        },
    }
    wrapper = ContentBoundRequestLedger(recovery_root / "ledger", wrapper_binding)
    wrapper.prepare()
    intent = {
        "schema_version": INTENT_SCHEMA,
        "predecessor_proof_sha256": sha(proof_raw),
        "authorization_sha256": sha(auth_raw),
        "wrapper_ledger_binding_sha256": wrapper.binding_sha256,
        "successor_engine_root": str(successor_engine.root),
        "successor_task_id": child_task.name,
        "successor_mode": _text(predecessor["mode"]),
        "successor_request_sha256": _text(predecessor["request_sha256"]),
        "max_additional_dispatches": 1,
    }
    store.immutable(recovery_root / "intent.json", canonical(intent))
    route = _object(predecessor["route"])
    capability_receipt = None
    original_snapshot = cast(JsonObject, original_engine._read_snapshot(
        original_engine._ledger(
            original_task,
            cast(JsonObject, strict_json(store.read(original_task / "manifest.json"))),
            _text(predecessor["mode"]),
            original_engine._body(_text(predecessor["task_id"]), child_inner, _text(predecessor["mode"])),
        )
    ))
    dispatch = cast(JsonObject, original_engine._dispatch_metadata(original_snapshot))
    capability_receipt = _optional_text(dispatch.get("capability_receipt_sha256"),
                                        "FORMAT_CAPABILITY_RECEIPT_INVALID")
    mode = _text(predecessor["mode"])
    policy = _FixedRoutePolicy(route, capability_receipt, child_task.name, mode)
    before = cast(JsonObject, successor_engine._accounting(child_task, child_manifest, child_inner))
    wrapper_snapshot = cast(JsonObject, wrapper.snapshot())
    if wrapper_snapshot["state"] == "PREPARED":
        wrapper.transition(
            "SUBMIT_DISPATCHED",
            event="FORMAT_RECOVERY_DISPATCH_RESERVED",
            metadata={
                "route": route,
                "requested_model": MODEL,
                "mode": mode,
                "predecessor_proof_sha256": sha(proof_raw),
                "authorization_sha256": sha(auth_raw),
                "successor_task_id": child_task.name,
                "dispatched_unix_s": now(),
                "deadline_unix_s": now() + max_batch_wait_s,
                "charge_status": "UNKNOWN",
            },
        )
    result = cast(JsonObject, successor_engine.advance(
        child_task.name,
        expected=expected,
        provider=provider,
        key_policy=cast(KeyPolicy, policy),
        allow_realtime_fallback=False,
        request_timeout_s=request_timeout_s,
        max_batch_wait_s=max_batch_wait_s,
        max_recovery_pages=max_recovery_pages,
        now=now,
        mode=mode,
        capability_binding_task=original_task,
    ))
    # The normal batch path reserves the POST and then performs one bounded
    # readback.  Complete this same successor's first readback when it is
    # still pending; this is query-only and cannot create another inference
    # reservation.  Other unknown states remain resumable by a later cold
    # call, just as the ordinary engine does.
    if result.get("status") == "PENDING":
        result = cast(JsonObject, successor_engine.advance(
            child_task.name,
            expected=expected,
            provider=provider,
            key_policy=cast(KeyPolicy, policy),
            allow_realtime_fallback=False,
            request_timeout_s=request_timeout_s,
            max_batch_wait_s=max_batch_wait_s,
            max_recovery_pages=max_recovery_pages,
            now=now,
            mode=mode,
            capability_binding_task=original_task,
        ))
    after = cast(JsonObject, successor_engine._accounting(child_task, child_manifest, child_inner))
    delta = _integer(after["inference_reservations"]) - _integer(before["inference_reservations"])
    require(0 <= delta <= 1, "FORMAT_RECOVERY_DISPATCH_LIMIT")
    before_completed = any(
        _text(_object(history)["mode"]) == mode and _text(_object(history)["state"]) == "COMPLETED"
        for history in _array(before["histories"])
    )
    after_completed = any(
        _text(_object(history)["mode"]) == mode and _text(_object(history)["state"]) == "COMPLETED"
        for history in _array(after["histories"])
    )
    new_usable_observations = int(
        result.get("status") == "OBSERVED" and after_completed and not before_completed
    )
    _wrapper_state_after_result(wrapper, successor_engine, child_task, child_manifest,
                                child_inner, mode, cast(JsonMap, result))
    child_ledger = successor_engine._ledger(child_task, child_manifest, mode,
                                            successor_engine._body(child_task.name, child_inner, mode))
    child_wire_schema_sha256 = _wire_schema_sha(child_task, _text(predecessor["request_sha256"]))
    require(child_wire_schema_sha256 == predecessor.get("wire_schema_sha256"),
            "FORMAT_SUCCESSOR_SCHEMA_DRIFT")
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "recovery_schema": RECOVERY_SCHEMA,
        "status": result["status"],
        "reason": result.get("reason"),
        "predecessor_proof_sha256": sha(proof_raw),
        "authorization_sha256": sha(auth_raw),
        "original": predecessor,
        "successor": {
            "engine_root": str(successor_engine.root),
            "task_id": child_task.name,
            "mode": predecessor["mode"],
            "requested_model": MODEL,
            "request_sha256": result.get("request_sha256"),
            "inner_request_sha256": result.get("inner_request_sha256"),
            "wire_schema_sha256": child_wire_schema_sha256,
            "response_sha256": result.get("response_sha256"),
            "result_sha256": result.get("result_sha256"),
            "ledger_sha256": sha(store.read(child_ledger.path)),
        },
        "wrapper_ledger_sha256": sha(store.read(wrapper.path)),
        "dispatches_this_recovery": delta,
    }
    receipt_path = recovery_root / "receipt.json"
    if receipt_path.exists():
        # A completed receipt is the immutable first terminal witness.  A
        # repeated cold call must return its original dispatch count rather
        # than rewrite it to zero after the successor ledger is terminal.
        existing = cast(JsonObject, strict_json(store.read(receipt_path)))
        existing_successor = _object(existing.get("successor", {}),
                                      "FORMAT_RECOVERY_RECEIPT_INVALID")
        require(existing == receipt or (
            existing.get("schema_version") == RECEIPT_SCHEMA
            and existing.get("predecessor_proof_sha256") == sha(proof_raw)
            and existing.get("authorization_sha256") == sha(auth_raw)
            and existing.get("status") == result["status"]
            and existing.get("reason") == result.get("reason")
            and existing_successor.get("result_sha256") == result.get("result_sha256")
            and existing_successor.get("response_sha256") == result.get("response_sha256")
        ), "FORMAT_RECOVERY_RECEIPT_DRIFT")
        receipt_sha256 = sha(store.read(receipt_path))
        receipt_dispatches = existing.get("dispatches_this_recovery")
    elif result["status"] in _NONTERMINAL_SUCCESSOR_STATUSES:
        receipt_sha256 = None
        receipt_dispatches = delta
    else:
        store.immutable(receipt_path, canonical(receipt))
        receipt_sha256 = sha(store.read(receipt_path))
        receipt_dispatches = delta
    metadata = {
        "schema_version": RECOVERY_SCHEMA,
        "root": str(recovery_root),
        "receipt_sha256": receipt_sha256,
        "predecessor_proof_sha256": sha(proof_raw),
        "authorization_sha256": sha(auth_raw),
        "successor_engine_root": str(successor_engine.root),
        "successor_task_id": child_task.name,
        "successor_mode": predecessor["mode"],
        "successor_result_sha256": result.get("result_sha256"),
        "successor_response_sha256": result.get("response_sha256"),
        "status": result["status"],
        "reason": result.get("reason"),
        "dispatches_this_recovery": receipt_dispatches,
    }
    return {
        "result": result,
        "format_recovery": metadata,
        "dispatches_this_advance": delta,
        "new_usable_observations_this_recovery": new_usable_observations,
    }


def validate_successor_envelope(
    metadata: JsonMap, *, result: JsonMap, expected: AudioSpec
) -> JsonObject:
    """Cold-consumer proof for a successor envelope; no trust in status flags."""

    require(isinstance(metadata, Mapping) and metadata.get("schema_version") == RECOVERY_SCHEMA,
            "FORMAT_ENVELOPE_SCHEMA_INVALID")
    root = Path(_text(metadata.get("root", ""), "FORMAT_ENVELOPE_ROOT_INVALID")).absolute()
    store.existing_directory(root)
    receipt_path = root / "receipt.json"
    receipt_raw = store.read(receipt_path)
    require(sha(receipt_raw) == _text(metadata.get("receipt_sha256"),
                                      "FORMAT_ENVELOPE_RECEIPT_DRIFT"),
            "FORMAT_ENVELOPE_RECEIPT_DRIFT")
    receipt = cast(JsonObject, strict_json(receipt_raw))
    # A normal successor is an OBSERVED hearing.  The one other admissible
    # terminal form is the exact flat-uncertainty response rejection for which
    # the successor has an independently sealed raw-replay sidecar.  The
    # sidecar is validated against the child engine below; a status flag alone
    # never grants entry to the ordinary consumer.
    raw_replay_successor = (
        receipt.get("status") == "RESPONSE_REJECTED"
        and receipt.get("reason") == "HEARING_UNCERTAINTIES_INVALID"
    )
    require(
        receipt.get("schema_version") == RECEIPT_SCHEMA
        and (receipt.get("status") == "OBSERVED" or raw_replay_successor),
        "FORMAT_ENVELOPE_NOT_OBSERVED",
    )
    require(metadata.get("status") == receipt.get("status")
            and metadata.get("reason") == receipt.get("reason")
            and metadata.get("dispatches_this_recovery") == receipt.get("dispatches_this_recovery"),
            "FORMAT_ENVELOPE_RECEIPT_METADATA_DRIFT")
    successor = _object(receipt.get("successor"), "FORMAT_ENVELOPE_SUCCESSOR_DRIFT")
    require(successor.get("result_sha256") == result.get("result_sha256"),
            "FORMAT_ENVELOPE_RESULT_DRIFT")
    require(successor.get("response_sha256") == result.get("response_sha256"),
            "FORMAT_ENVELOPE_RESPONSE_DRIFT")
    proof_raw = store.read(root / "predecessor-proof.json")
    require(sha(proof_raw) == _text(receipt.get("predecessor_proof_sha256"),
                                    "FORMAT_ENVELOPE_PROOF_DRIFT"),
            "FORMAT_ENVELOPE_PROOF_DRIFT")
    proof = cast(JsonObject, strict_json(proof_raw))
    auth_raw = store.read(root / "authorization.json")
    require(sha(auth_raw) == _text(receipt.get("authorization_sha256"),
                                   "FORMAT_ENVELOPE_AUTH_DRIFT"),
            "FORMAT_ENVELOPE_AUTH_DRIFT")
    auth = cast(JsonObject, strict_json(auth_raw))
    validate_authorization(auth, proof)
    require(receipt.get("original") == proof, "FORMAT_ENVELOPE_ORIGINAL_DRIFT")
    require(successor.get("engine_root") == metadata.get("successor_engine_root")
            and successor.get("task_id") == metadata.get("successor_task_id")
            and successor.get("mode") == metadata.get("successor_mode")
            and successor.get("request_sha256") == proof.get("request_sha256")
            and successor.get("inner_request_sha256") == proof.get("inner_request_sha256")
            and successor.get("wire_schema_sha256") == proof.get("wire_schema_sha256"),
            "FORMAT_ENVELOPE_SUCCESSOR_DRIFT")
    require(metadata.get("successor_result_sha256") == successor.get("result_sha256")
            and metadata.get("successor_response_sha256") == successor.get("response_sha256"),
            "FORMAT_ENVELOPE_SUCCESSOR_RESULT_DRIFT")
    from .argon_escalation_witness import ArgonEscalationWitness

    successor_engine = ArgonEscalationWitness(
        Path(_text(successor["engine_root"])), read_only=True
    )
    child_task, child_manifest, child_inner = successor_engine._load(
        _text(successor["task_id"]), expected
    )
    child_manifest = cast(JsonObject, child_manifest)
    child_ledger = successor_engine._ledger(
        child_task,
        child_manifest,
        _text(successor["mode"]),
        successor_engine._body(child_task.name, child_inner, _text(successor["mode"])),
    )
    require(sha(store.read(child_ledger.path)) == _text(successor.get("ledger_sha256"),
                                                         "FORMAT_ENVELOPE_SUCCESSOR_LEDGER_DRIFT"),
            "FORMAT_ENVELOPE_SUCCESSOR_LEDGER_DRIFT")
    if raw_replay_successor:
        # ``result`` is supplied by the ordinary witness consumer, but this
        # validator must not trust a copied ``raw_replay`` member.  Re-read
        # the immutable child receipt/projection/derived result and original
        # terminal seal through the existing raw-replay validator instead.
        require(
            result.get("status") == "RESPONSE_REJECTED"
            and result.get("reason") == "HEARING_UNCERTAINTIES_INVALID",
            "FORMAT_ENVELOPE_RAW_REPLAY_RESULT_INVALID",
        )
        from .argon_raw_replay import read_raw_replay, raw_replay_is_observed

        child_snapshot = cast(JsonObject, successor_engine._read_snapshot(child_ledger))
        replay = read_raw_replay(
            successor_engine,
            child_task,
            child_manifest,
            child_inner,
            _text(successor["mode"]),
            result,
            snapshot=child_snapshot,
        )
        require(
            replay is not None and raw_replay_is_observed(replay),
            "FORMAT_ENVELOPE_RAW_REPLAY_INVALID",
        )
    require(
        _wire_schema_sha(child_task, _text(successor["request_sha256"]))
        == _text(proof.get("wire_schema_sha256")),
        "FORMAT_ENVELOPE_SUCCESSOR_SCHEMA_DRIFT",
    )
    intent = cast(JsonObject, strict_json(store.read(root / "intent.json")))
    require(intent.get("schema_version") == INTENT_SCHEMA
            and intent.get("predecessor_proof_sha256") == receipt.get("predecessor_proof_sha256")
            and intent.get("authorization_sha256") == receipt.get("authorization_sha256")
            and intent.get("successor_task_id") == successor.get("task_id")
            and intent.get("successor_mode") == successor.get("mode")
            and intent.get("successor_request_sha256") == successor.get("request_sha256")
            and intent.get("max_additional_dispatches") == 1,
            "FORMAT_ENVELOPE_INTENT_DRIFT")
    wrapper_dir = root / "ledger"
    store.existing_directory(wrapper_dir)
    wrapper_files = sorted(wrapper_dir.glob("*.json"))
    require(len(wrapper_files) == 1
            and sha(store.read(wrapper_files[0])) == _text(receipt.get("wrapper_ledger_sha256"),
                                                          "FORMAT_ENVELOPE_WRAPPER_DRIFT"),
            "FORMAT_ENVELOPE_WRAPPER_DRIFT")
    original_engine = ArgonEscalationWitness(Path(_text(proof["engine_root"])), read_only=True)
    task, manifest_raw, inner = original_engine._load(_text(proof["task_id"]), expected)
    manifest = cast(JsonObject, manifest_raw)
    proof_mode = _text(proof["mode"])
    original_ledger = original_engine._ledger(task, manifest, proof_mode,
                                              original_engine._body(task.name, inner, proof_mode))
    require(sha(store.read(original_ledger.path)) == _text(proof["ledger_sha256"]),
            "FORMAT_ENVELOPE_ORIGINAL_LEDGER_DRIFT")
    require(sha(inner) == proof["inner_request_sha256"]
            and sha(canonical(_object(manifest["binding"]))) == _text(proof["binding_sha256"]),
            "FORMAT_ENVELOPE_ORIGINAL_BINDING_DRIFT")
    store.get_blob(task, _text(proof["response_sha256"]))
    store.get_blob(task, _text(proof["result_sha256"]))
    return receipt


__all__ = [
    "AUTHORIZATION_SCHEMA",
    "FORMAT_FAILURE_REASONS",
    "PREDECESSOR_SCHEMA",
    "RECOVERY_SCHEMA",
    "RECEIPT_SCHEMA",
    "INTENT_SCHEMA",
    "make_authorization",
    "predecessor_proof",
    "validate_authorization",
    "recover",
    "validate_successor_envelope",
]
