"""Read-only native Argon completion into the ordinary scoped CPA contract.

Never submits, polls, resolves credentials or constructs another provider's
seal. Native operation, raw model identity and full submitted audio are replayed
by the same engine used by ordinary Argon consumers on every cold consumption.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Mapping

from . import argon_escalation_storage as store
from .argon_escalation_contract import canonical, file_sha, require, sha, strict_json, valid_sha
from .argon_escalation_integration import (
    EVIDENCE_SCHEMA,
    _check_request,
    material_identity,
    read_ordinary_history,
    spec_from_envelope,
    transcript_witness,
    validate_consumer_evidence,
)
from .argon_escalation_witness import ArgonEscalationWitness

SCHEMA = "argon-native-span-upgrade.v1"


def _state(request: Mapping, envelope: Mapping) -> dict:
    from .span_phoneme_resolution import edit_binding

    binding = edit_binding(request)
    require(binding is not None, "ARGON_SPAN_BINDING_REQUIRED")
    result = validate_consumer_evidence(request, envelope)
    return {
        "schema_version": SCHEMA,
        "enabled": True,
        "status": "COMPLETED",
        "edit_binding_sha256": binding["binding_sha256"],
        "submission_outcome": "COMPLETED",
        "native_receipt_sha256": result["result_sha256"],
        "native_identity": {
            "provider": result.get("provider", "gemini_api"),
            **({"selected_model_label": result["selected_model_label"],
                "model_identity_basis": result["model_identity_basis"]} if result["mode"] == "agy" else {}),
            "model_version": result["model_version"],
            "task_id": result["task_id"],
            "job_name": result["job_name"],
            "mode": result["mode"],
            "request_sha256": result["request_sha256"],
            "inner_request_sha256": result["inner_request_sha256"],
            "response_sha256": result["response_sha256"],
            "binding": deepcopy(result["binding"]),
        },
        "observations": [transcript_witness(request, envelope)],
    }


def validate_upgrade(request: Mapping, state: Mapping) -> dict:
    require(state.get("schema_version") == SCHEMA, "ARGON_SPAN_UPGRADE_SCHEMA_INVALID")
    observations = state.get("observations")
    require(
        isinstance(observations, list) and len(observations) == 1,
        "ARGON_SPAN_UPGRADE_OBSERVATION_INVALID",
    )
    envelope = observations[0].get("argon_escalation_evidence")
    require(isinstance(envelope, Mapping), "ARGON_SPAN_NATIVE_EVIDENCE_REQUIRED")
    rebuilt = _state(request, envelope)
    require(canonical(state) == canonical(rebuilt), "ARGON_SPAN_NATIVE_STATE_DRIFT")
    return rebuilt


def read_upgrade(integration, request: Mapping) -> dict:
    """Normal verifier callback: inspect exact persisted task, never advance it."""
    from .span_phoneme_resolution import edit_binding

    binding = edit_binding(request)
    base = {
        "schema_version": SCHEMA,
        "enabled": True,
        "status": "AVAILABLE",
        "edit_binding_sha256": (binding or {}).get("binding_sha256"),
        "observations": [],
    }
    try:
        _check_request(request)
        require(
            file_sha(integration.source) == integration.source_sha, "ARGON_RUNTIME_SOURCE_DRIFT"
        )
        identifier = request.get("argon_finding_id")
        if identifier is None:
            return base  # Not yet scoped/submitted, not an exhausted upgrade.
        require(valid_sha(identifier), "ARGON_SPAN_FINDING_INVALID")
        root = integration.runtime.base / "cache/argon-consumer" / identifier
        if not root.exists():
            return base
        store.existing_directory(root)
        original = store.get_json(root / "source-binding.json")
        # The shared finding record names where its first run read the media;
        # the source itself is its bytes (current bytes checked above).
        require(
            original["source_media_sha256"] == integration.source_sha,
            "ARGON_SPAN_SOURCE_DRIFT",
        )
        handles = sorted(
            (root / "batch").glob("attempt-*/handle.json"),
            key=lambda p: int(p.parent.name.split("-")[-1]),
        )
        if not handles:
            return {
                **base,
                "status": "GRANTED" if (root / "trigger.json").exists() else "AVAILABLE",
            }
        handle = store.get_json(handles[-1])
        engine = ArgonEscalationWitness(handles[-1].parent / "engine", read_only=True)
        envelope = {
            "schema_version": EVIDENCE_SCHEMA,
            "consumer_root": str(root),
            "source_binding": original,
            "engine_root": str(engine.root),
            "task_id": handle["task_id"],
        }
        spec = spec_from_envelope(envelope)
        inspected = engine.read_status(handle["task_id"], expected=spec)
        result = inspected["result"]
        if result is None or result.get("status") != "OBSERVED":
            return {
                **base,
                "status": result["status"] if result else inspected["state"],
                "submission_outcome": inspected["state"],
                "job_name": inspected["job_name"],
                "task_id": handle["task_id"],
                "ledger_sha256": inspected["ledger_sha256"],
            }
        require(inspected["state"] == "COMPLETED", "ARGON_SPAN_SUBMISSION_INCOMPLETE")
        envelope["result_sha256"] = inspected["result_sha256"]
        envelope["envelope_sha256"] = sha(canonical(envelope))
        return _state(request, envelope)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        return {
            **base,
            "status": "STATE_INVALID",
            "error_type": type(exc).__name__,
            "reason_code": getattr(exc, "reason", None),
        }


def catalogue_observation(request: Mapping, row: Mapping) -> dict:
    """Project verified native target text, retaining Google identity and clocks."""
    envelope = row.get("argon_escalation_evidence")
    require(isinstance(envelope, Mapping), "ARGON_SPAN_NATIVE_EVIDENCE_REQUIRED")
    result = validate_consumer_evidence(request, envelope)
    expected = transcript_witness(request, envelope)
    require(canonical(row) == canonical(expected), "ARGON_SPAN_OBSERVATION_DRIFT")
    identity = material_identity(request, envelope)
    return {
        "observation_id": sha(canonical(identity)),
        "kind": "argon_native_transcript",
        "language": "native_unlabelled",
        "text": result["hearing"]["transcript"],
        "provider": result.get("provider", "gemini_api"),
        "model": result.get("selected_model_label") if result["mode"] == "agy" else result["model_version"],
        "route_origin": "upgrade",
        "source_media_sha256": identity["source_media_sha256"],
        "audio_clip_sha256": identity["audio_clip_sha256"],
        "response_sha256": result["response_sha256"],
        "acquisition_request_sha256": identity["acquisition_request_sha256"],
        "native_operation": {
            k: result[k] for k in ("task_id", "job_name", "mode", "request_sha256")
        },
        "physical_audio_window_ms": result["binding"]["context_source_ms"],
        "physical_target_window_ms": result["binding"]["target_source_ms"],
        "word_timing_measured": False,
        "witness_request": deepcopy(request),
        "raw_observation": deepcopy(dict(row)),
    }


def original_hearings(request: Mapping, envelope: Mapping) -> list[dict]:
    """Use actual validated old manifests, not Argon as a completed primary route."""
    from .span_phoneme_resolution import _hash

    validate_consumer_evidence(request, envelope)
    root, spec = Path(envelope["consumer_root"]), spec_from_envelope(envelope)
    result = {}
    trigger = store.get_json(root / "trigger.json")
    if trigger.get("schema_version") == "argon-targeted-relisten-trigger.v1":
        from .argon_targeted_relisten import validate_trigger
        from .span_phoneme_resolution import observation_catalogue
        proof = validate_trigger(root, spec, trigger, current_check=request)
        prior, _ = observation_catalogue(proof["check"], {})
        for row in prior:
            if row["kind"] == "blind_pinyin":
                result[row["observation_id"]] = {**deepcopy(row), "route_origin": "primary_original_hearing"}
    for _, _, _, event in read_ordinary_history(root, spec):
        ordinary = event["ordinary"]
        manifest = strict_json(store.get_blob(root, ordinary["artifacts"]["manifest"]))
        row = manifest["verdict"]
        if row.get("status") != "OBSERVED":
            continue
        # read_ordinary_history validates the original raw protocol, source,
        # prompt, audio and clocks against the immutable native acquisition.
        raw = strict_json(store.get_blob(root, ordinary["artifacts"]["response"]))
        if not isinstance(raw.get("heard_pinyin"), str) or not raw["heard_pinyin"].strip():
            continue
        identity = sha(
            canonical(
                {
                    "manifest_sha256": ordinary["artifacts"]["manifest"],
                    "response_sha256": manifest["response_sha256"],
                }
            )
        )
        physical = manifest["timeline_binding"]["source_media"]
        result[identity] = {
            "observation_id": identity,
            "kind": "blind_pinyin",
            "language": "zh",
            "text": raw["heard_pinyin"],
            "provider": manifest["provider"],
            "model": manifest["model"],
            "source_media_sha256": _hash(manifest["source_media_sha256"]),
            "audio_clip_sha256": _hash(manifest["audio_clip_sha256"]),
            "response_sha256": _hash(manifest["response_sha256"]),
            "physical_audio_window_ms": [physical["crop_start_ms"], physical["crop_end_ms"]],
            "witness_request": deepcopy(ordinary["witness_request"]),
            "raw_observation": deepcopy(row),
            "route_origin": "primary_original_hearing",
            "original_manifest_sha256": ordinary["artifacts"]["manifest"],
        }
    return list(result.values())
