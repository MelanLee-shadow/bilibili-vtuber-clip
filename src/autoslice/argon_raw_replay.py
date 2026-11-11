"""Pure projection for one retained Argon response representation defect.

The native hearing contract remains strict.  This module only recognizes the
one observed legacy wire shape: a repeated six-item sequence of
``start_ms, integer, end_ms, integer, reason, non-empty string``.  It copies
those values into the canonical span-object representation and runs the
existing strict parser afterwards.  It never changes status or source binding.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Mapping, Protocol, cast

from . import argon_escalation_storage as storage
from .argon_escalation_contract import (
    ContractError,
    MODEL,
    canonical,
    parse_hearing_document,
    require,
    sha,
    strict_json,
    valid_sha,
)
from .cpa_gemini_client import JsonMap, JsonObject, JsonValue
from .content_bound_request_ledger import ContentBoundRequestLedger


RAW_REPLAY_SCHEMA = "argon-raw-replay.v1"
RAW_REPLAY_RESULT_SCHEMA = "argon-raw-replay-result.v1"
RAW_REPLAY_RECEIPT_SCHEMA = "argon-raw-replay-receipt.v1"
RAW_REPLAY_RULE_VERSION = "uncertainties-flat-six.v1"
RAW_REPLAY_OBSERVATION_STATUS = "OBSERVED"


class RawReplayEngine(Protocol):
    """The existing witness operations needed to validate retained JSON."""

    read_only: bool

    def _body(self, task_id: str, inner: bytes, mode: str) -> bytes: ...
    def _ledger(self, task: Path, manifest: JsonObject, mode: str,
                body: bytes) -> ContentBoundRequestLedger: ...
    def _read_snapshot(self, ledger: ContentBoundRequestLedger) -> JsonObject: ...
    def _job_name(self, snapshot: JsonObject) -> str | None: ...
    def _decode(self, raw: bytes, task_id: str, binding: JsonObject, mode: str,
                job_name: str | None) -> tuple[JsonObject, JsonObject | None, str | None]: ...


def _object(value: JsonValue, reason: str) -> JsonObject:
    if isinstance(value, dict):
        return value
    require(False, reason)
    raise AssertionError(reason)


def _array(value: JsonValue, reason: str) -> list[JsonValue]:
    if isinstance(value, list):
        return value
    require(False, reason)
    raise AssertionError(reason)


def _text(value: JsonValue, reason: str) -> str:
    if isinstance(value, str):
        return value
    require(False, reason)
    raise AssertionError(reason)


def _nonempty_text(value: JsonValue, reason: str) -> str:
    value = _text(value, reason)
    require(bool(value.strip()), reason)
    return value


def _sha_text(value: object, reason: str) -> str:
    require(valid_sha(value), reason)
    return cast(str, value)


def _integer(value: JsonValue, reason: str) -> int:
    if type(value) is int:
        return value
    require(False, reason)
    raise AssertionError(reason)


def _terminal_metadata(snapshot: JsonMap) -> JsonObject:
    reason = "RAW_REPLAY_ORIGINAL_TERMINAL_DRIFT"
    events = _array(snapshot.get("events"), reason)
    require(bool(events), reason)
    return _object(_object(events[-1], reason).get("metadata"), reason)


def project_hearing_document(heard: JsonMap, binding: JsonMap) -> JsonObject:
    """Project the exact retained flat uncertainty shape into strict hearing JSON.

    This is deliberately not part of ``parse_hearing_document``.  Callers must
    first prove the original terminal response failed only with the permitted
    representation reason and must persist the projection separately.
    """

    source = _object(cast(JsonValue, heard), "RAW_REPLAY_DOCUMENT_INVALID")
    flat = _array(source.get("uncertainties"), "RAW_REPLAY_SHAPE_INVALID")
    require(bool(flat) and len(flat) % 6 == 0, "RAW_REPLAY_SHAPE_INVALID")
    projected: JsonObject = deepcopy(source)
    spans: list[JsonValue] = []
    for index in range(0, len(flat), 6):
        require(flat[index] == "start_ms", "RAW_REPLAY_SHAPE_INVALID")
        require(flat[index + 2] == "end_ms", "RAW_REPLAY_SHAPE_INVALID")
        require(flat[index + 4] == "reason", "RAW_REPLAY_SHAPE_INVALID")
        start = _integer(flat[index + 1], "RAW_REPLAY_SHAPE_INVALID")
        end = _integer(flat[index + 3], "RAW_REPLAY_SHAPE_INVALID")
        reason = _nonempty_text(flat[index + 5], "RAW_REPLAY_SHAPE_INVALID")
        spans.append({"start_ms": start, "end_ms": end, "reason": reason})
    projected["uncertainties"] = spans
    require(projected.get("status") == "UNCERTAIN", "RAW_REPLAY_STATUS_INVALID")
    parsed = parse_hearing_document(projected, dict(binding))
    require(parsed.get("status") == source.get("status") == "UNCERTAIN",
            "RAW_REPLAY_STATUS_INVALID")
    require(parsed.get("binding_sha256") == source.get("binding_sha256"),
            "RAW_REPLAY_BINDING_DRIFT")
    return parsed


def hearing_document_from_payload(payload: JsonMap, mode: str) -> JsonObject:
    """Extract the document without applying the strict hearing schema."""

    if mode in {"agy", "realtime"}:
        return cast(JsonObject, dict(payload))
    require(payload.get("modelVersion") == MODEL, "RAW_REPLAY_PAYLOAD_INVALID")
    candidates = _array(payload.get("candidates"), "RAW_REPLAY_PAYLOAD_INVALID")
    require(len(candidates) == 1, "RAW_REPLAY_PAYLOAD_INVALID")
    candidate = _object(candidates[0], "RAW_REPLAY_PAYLOAD_INVALID")
    require(candidate.get("finishReason") == "STOP", "RAW_REPLAY_PAYLOAD_INVALID")
    content = _object(candidate.get("content"), "RAW_REPLAY_PAYLOAD_INVALID")
    parts = _array(content.get("parts"), "RAW_REPLAY_PAYLOAD_INVALID")
    require(bool(parts), "RAW_REPLAY_PAYLOAD_INVALID")
    texts: list[str] = []
    for part in parts:
        part = _object(part, "RAW_REPLAY_PAYLOAD_INVALID")
        if part.get("thought") is True:
            continue
        texts.append(_text(part.get("text"), "RAW_REPLAY_PAYLOAD_INVALID"))
    require(bool(texts), "RAW_REPLAY_PAYLOAD_INVALID")
    return strict_json("".join(texts))


def raw_replay_result(
    *,
    task_id: str,
    mode: str,
    binding: JsonMap,
    original_result_sha256: str,
    original_response_sha256: str,
    raw_sha256: str,
    hearing: JsonMap,
) -> JsonObject:
    """Build the immutable derived result; no provider or ledger mutation."""

    value: JsonObject = {
        "schema_version": RAW_REPLAY_RESULT_SCHEMA,
        "rule_version": RAW_REPLAY_RULE_VERSION,
        "task_id": task_id,
        "mode": mode,
        "status": "UNCERTAIN",
        # This is transport/evidence observation, not a hearing certainty.
        # The hearing itself remains UNCERTAIN and retains its uncertainties.
        "observation_status": RAW_REPLAY_OBSERVATION_STATUS,
        "reason": "RAW_REPLAY_FORMAT_REVALIDATED",
        "finding_resolved": False,
        "mutation_authorized": False,
        "release_authorized": False,
        "requires_cpa": True,
        "binding": deepcopy(dict(binding)),
        "binding_sha256": sha(canonical(binding)),
        "original_result_sha256": original_result_sha256,
        "original_response_sha256": original_response_sha256,
        "raw_sha256": raw_sha256,
        "hearing": deepcopy(dict(hearing)),
        "dispatches": 0,
        "provider_calls": 0,
    }
    return value


def raw_replay_is_observed(value: object) -> bool:
    """Recognize a validated replay as observed raw evidence, not certainty."""

    if not isinstance(value, Mapping):
        return False
    hearing = value.get("hearing")
    return bool(
        value.get("schema_version") == RAW_REPLAY_SCHEMA
        and value.get("rule_version") == RAW_REPLAY_RULE_VERSION
        and value.get("status") == "UNCERTAIN"
        and value.get("observation_status") == RAW_REPLAY_OBSERVATION_STATUS
        and value.get("reason") == "RAW_REPLAY_FORMAT_REVALIDATED"
        and value.get("dispatches") == 0
        and value.get("provider_calls") == 0
        and isinstance(hearing, Mapping)
        and hearing.get("status") == "UNCERTAIN"
    )


def raw_replay_payload(
    engine: RawReplayEngine,
    task: Path,
    manifest: JsonMap,
    mode: str,
    result: JsonMap,
    snapshot: JsonMap,
) -> tuple[bytes, JsonObject]:
    """Re-read and project the exact retained predecessor; never dispatch."""

    require(
        result.get("status") == "RESPONSE_REJECTED"
        and result.get("reason") == "HEARING_UNCERTAINTIES_INVALID",
        "RAW_REPLAY_ORIGINAL_FAILURE_INVALID",
    )
    response_sha256 = _sha_text(
        result.get("response_sha256"), "RAW_REPLAY_RESPONSE_SHA_INVALID"
    )
    require(
        _terminal_metadata(snapshot).get("response_sha256") == response_sha256,
        "RAW_REPLAY_ORIGINAL_TERMINAL_DRIFT",
    )
    raw = storage.get_blob(task, response_sha256)
    payload, heard, reason = engine._decode(
        raw, task.name, _object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID"),
        mode, engine._job_name(dict(snapshot)),
    )
    require(
        heard is None and reason == "HEARING_UNCERTAINTIES_INVALID",
        "RAW_REPLAY_ORIGINAL_FAILURE_INVALID",
    )
    hearing = hearing_document_from_payload(payload, mode)
    return raw, project_hearing_document(hearing, _object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID"))


def raw_replay_paths(task: Path) -> tuple[Path, Path]:
    replay_root = task / "raw-replay"
    return replay_root, replay_root / "receipt.json"


def validate_raw_replay_predecessor(
    engine: RawReplayEngine,
    task: Path,
    manifest: JsonMap,
    inner: bytes,
    mode: str,
    result: JsonMap,
    *,
    snapshot: JsonObject | None = None,
) -> tuple[JsonObject, str, str]:
    """Verify the sealed old result before trusting any derived sidecar."""

    original_result_sha256 = _sha_text(
        result.get("result_sha256"), "RAW_REPLAY_ORIGINAL_HASH_INVALID"
    )
    original_response_sha256 = _sha_text(
        result.get("response_sha256"), "RAW_REPLAY_ORIGINAL_HASH_INVALID"
    )
    if snapshot is None:
        ledger = engine._ledger(task, dict(manifest), mode, engine._body(task.name, inner, mode))
        snapshot = engine._read_snapshot(ledger)
    terminal = _terminal_metadata(snapshot)
    require(
        terminal.get("result_sha256") == original_result_sha256
        and terminal.get("response_sha256") == original_response_sha256,
        "RAW_REPLAY_ORIGINAL_TERMINAL_DRIFT",
    )
    sealed = strict_json(storage.get_blob(task, original_result_sha256))
    supplied = {
        key: value
        for key, value in result.items()
        if key not in {"result_sha256", "accounting", "raw_replay"}
    }
    require(canonical(supplied) == canonical(sealed), "RAW_REPLAY_ORIGINAL_RESULT_DRIFT")
    return snapshot, original_result_sha256, original_response_sha256


def read_raw_replay(
    engine: RawReplayEngine,
    task: Path,
    manifest: JsonMap,
    inner: bytes,
    mode: str,
    result: JsonMap,
    *,
    snapshot: JsonObject | None = None,
) -> JsonObject | None:
    """Cold-read and revalidate an independently stored replay sidecar."""

    _, receipt_path = raw_replay_paths(task)
    snapshot, original_result_sha256, original_response_sha256 = (
        validate_raw_replay_predecessor(
            engine, task, manifest, inner, mode, result, snapshot=snapshot
        )
    )
    if not receipt_path.exists():
        return None
    receipt = cast(JsonObject, storage.get_json(receipt_path))
    require(
        set(receipt)
        == {
            "schema_version",
            "rule_version",
            "task_id",
            "mode",
            "binding",
            "binding_sha256",
            "original_result_sha256",
            "original_response_sha256",
            "raw_sha256",
            "projection_sha256",
            "result_sha256",
            "status",
            "observation_status",
            "dispatches",
            "provider_calls",
        },
        "RAW_REPLAY_RECEIPT_FIELDS_INVALID",
    )
    require(
        receipt["schema_version"] == RAW_REPLAY_RECEIPT_SCHEMA
        and receipt["rule_version"] == RAW_REPLAY_RULE_VERSION
        and receipt["task_id"] == task.name
        and receipt["mode"] == mode
        and receipt["binding"] == _object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID")
        and receipt["binding_sha256"] == sha(canonical(_object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID")))
        and receipt["status"] == "UNCERTAIN"
        and receipt["observation_status"] == RAW_REPLAY_OBSERVATION_STATUS
        and receipt["dispatches"] == 0
        and receipt["provider_calls"] == 0,
        "RAW_REPLAY_RECEIPT_BINDING_INVALID",
    )
    require(
        receipt["original_result_sha256"] == original_result_sha256
        and receipt["original_response_sha256"] == original_response_sha256,
        "RAW_REPLAY_ORIGINAL_HASH_INVALID",
    )
    raw, projected = raw_replay_payload(engine, task, manifest, mode, result, snapshot)
    require(
        receipt["raw_sha256"] == original_response_sha256 == sha(raw),
        "RAW_REPLAY_RAW_DRIFT",
    )
    projection_sha256 = _sha_text(
        receipt["projection_sha256"], "RAW_REPLAY_DERIVED_HASH_INVALID"
    )
    result_sha256 = _sha_text(
        receipt["result_sha256"], "RAW_REPLAY_DERIVED_HASH_INVALID"
    )
    projection_raw = storage.get_blob(task, projection_sha256)
    stored_projection = strict_json(projection_raw)
    require(
        sha(projection_raw) == projection_sha256
        and canonical(stored_projection) == canonical(projected),
        "RAW_REPLAY_PROJECTION_DRIFT",
    )
    derived_raw = storage.get_blob(task, result_sha256)
    derived = strict_json(derived_raw)
    expected_derived = raw_replay_result(
        task_id=task.name,
        mode=mode,
        binding=_object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID"),
        original_result_sha256=original_result_sha256,
        original_response_sha256=original_response_sha256,
        raw_sha256=sha(raw),
        hearing=projected,
    )
    require(
        sha(derived_raw) == result_sha256
        and canonical(derived) == canonical(expected_derived),
        "RAW_REPLAY_RESULT_DRIFT",
    )
    replay: JsonObject = {
        "schema_version": RAW_REPLAY_SCHEMA,
        "rule_version": RAW_REPLAY_RULE_VERSION,
        "task_id": task.name,
        "mode": mode,
        "status": "UNCERTAIN",
        "observation_status": RAW_REPLAY_OBSERVATION_STATUS,
        "reason": "RAW_REPLAY_FORMAT_REVALIDATED",
        "binding_sha256": receipt["binding_sha256"],
        "original_result_sha256": original_result_sha256,
        "original_response_sha256": original_response_sha256,
        "raw_sha256": receipt["raw_sha256"],
        "projection_sha256": projection_sha256,
        "result_sha256": result_sha256,
        "receipt_sha256": sha(storage.read(receipt_path)),
        "dispatches": 0,
        "provider_calls": 0,
        "hearing": projected,
    }
    require(raw_replay_is_observed(replay), "RAW_REPLAY_RESULT_DRIFT")
    return replay


def create_raw_replay(
    engine: RawReplayEngine,
    task: Path,
    manifest: JsonMap,
    inner: bytes,
    mode: str,
    result: JsonMap,
    *,
    snapshot: JsonObject | None = None,
) -> JsonObject | None:
    """Create the sidecar once, without changing the old result or ledger."""

    existing = read_raw_replay(
        engine, task, manifest, inner, mode, result, snapshot=snapshot
    )
    if existing is not None:
        return existing
    require(not engine.read_only, "READ_ONLY_ENGINE")
    if snapshot is None:
        ledger = engine._ledger(task, dict(manifest), mode, engine._body(task.name, inner, mode))
        snapshot = engine._read_snapshot(ledger)
    try:
        raw, projected = raw_replay_payload(engine, task, manifest, mode, result, snapshot)
    except ContractError as error:
        # Only the exact six-item legacy representation is eligible. Other
        # malformed or incomplete responses remain their original failures.
        if error.reason in {
            "RAW_REPLAY_SHAPE_INVALID",
            "RAW_REPLAY_STATUS_INVALID",
            "RAW_REPLAY_BINDING_DRIFT",
            "RAW_REPLAY_ORIGINAL_FAILURE_INVALID",
            "HEARING_UNCERTAINTIES_INVALID",
        }:
            return None
        raise
    original_result_sha256 = _sha_text(
        result.get("result_sha256"), "RAW_REPLAY_ORIGINAL_HASH_INVALID"
    )
    original_response_sha256 = _sha_text(
        result.get("response_sha256"), "RAW_REPLAY_ORIGINAL_HASH_INVALID"
    )
    projection_raw = canonical(projected)
    projection_sha256 = sha(projection_raw)
    derived = raw_replay_result(
        task_id=task.name,
        mode=mode,
        binding=_object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID"),
        original_result_sha256=original_result_sha256,
        original_response_sha256=original_response_sha256,
        raw_sha256=sha(raw),
        hearing=projected,
    )
    derived_raw = canonical(derived)
    result_sha256 = sha(derived_raw)
    receipt = {
        "schema_version": RAW_REPLAY_RECEIPT_SCHEMA,
        "rule_version": RAW_REPLAY_RULE_VERSION,
        "task_id": task.name,
        "mode": mode,
        "binding": _object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID"),
        "binding_sha256": sha(canonical(_object(manifest.get("binding"), "RAW_REPLAY_BINDING_INVALID"))),
        "original_result_sha256": original_result_sha256,
        "original_response_sha256": original_response_sha256,
        "raw_sha256": sha(raw),
        "projection_sha256": projection_sha256,
        "result_sha256": result_sha256,
        "status": "UNCERTAIN",
        "observation_status": RAW_REPLAY_OBSERVATION_STATUS,
        "dispatches": 0,
        "provider_calls": 0,
    }
    replay_root, receipt_path = raw_replay_paths(task)
    storage.directory(replay_root)
    storage.immutable(task / "blobs" / f"{projection_sha256}.bin", projection_raw)
    storage.immutable(task / "blobs" / f"{result_sha256}.bin", derived_raw)
    storage.immutable(receipt_path, canonical(receipt))
    return read_raw_replay(
        engine, task, manifest, inner, mode, result, snapshot=snapshot
    )
