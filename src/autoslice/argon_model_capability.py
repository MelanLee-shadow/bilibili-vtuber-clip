"""Metadata-only Argon capability admission, separate from inference accounting.

Each task keeps one immutable intent/raw/result per route. Unknown metadata never
authorizes inference, API rotation, quota strikes, or paid use. Cold consumers
reparse the retained response against the current task/scope/model binding.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

from . import argon_escalation_storage as store
from .argon_escalation_contract import MODEL, canonical, file_sha, require, sha, strict_json
from .argon_escalation_witness import ProviderTransportError

AGY_LABEL = "Gemini 4 Argon"
API_PATH = "models/" + MODEL
API_ORIGIN = "https://generativelanguage.googleapis.com/v1beta/"
SCHEMA = "argon-model-capability.v1"
# v2 API receipts bind the key tier's inference availability. models.get on a
# key only proves the model is listed for that key, never that the key's tier
# may run inference on it (OCI3: free key metadata 200, then a
# zero-inference HTTP 400 FAILED_PRECONDITION on the first Argon submit).
API_SCHEMA = "argon-model-capability.v2"
# Provider tier table for this exact model. Source: Google Gemini API pricing
# page (https://ai.google.dev/gemini-api/docs/pricing, read):
# "Gemini 4 Argon is not available on the Free tier."; every Argon table lists
# Free Tier "Not available", Paid tier Standard/Batch/Flex/Priority. Same fact
# in the research notes (official-sources.md: "无 Free tier").
TIER_INFERENCE_BASIS = "GOOGLE_GEMINI_API_PRICING_2026-10-21_ARGON_NOT_ON_FREE_TIER"
TIER_INFERENCE = {"free": "UNAVAILABLE", "paid_backup": "AVAILABLE"}


def tier_inference(tier):
    """Inference availability of one key tier for the Argon model."""
    status = TIER_INFERENCE.get(tier, "UNKNOWN")
    return {"tier": tier, "status": status, "basis": TIER_INFERENCE_BASIS}


def tier_available(tier):
    return tier_inference(tier)["status"] == "AVAILABLE"


class CapabilityBlocked(ValueError):
    def __init__(self, receipt):
        self.receipt = receipt
        self.status = ("CAPABILITY_UNKNOWN" if receipt["status"] == "UNKNOWN"
                       else "FAILED_PRE_DISPATCH")
        super().__init__(receipt["reason"])


def scope_binding(task, *, allow_legacy_unsealed=False):
    root = task.parents[2]
    path = root / "routing-policy.json"
    if not os.path.lexists(path):
        return {"scope": "engine_only", "routing_policy_sha256": None}
    raw = store.read(path)
    routing = strict_json(raw)
    require(routing["scope"] in {"production", "comparison", "legacy_api_only"},
            "ARGON_ROUTING_POLICY_DRIFT")
    require(routing["schema_version"] in {"argon-routing-policy.v1", "argon-routing-policy.v2"},
            "ARGON_ROUTING_POLICY_DRIFT")
    seal_path = root / "routing-policy-seal.json"
    if not os.path.lexists(seal_path):
        # Only original v1 dispatches with no capability fields/artifacts may
        # be read without rewriting their historical routing policy.
        require(allow_legacy_unsealed
                and routing["schema_version"] == "argon-routing-policy.v1",
                "ARGON_ROUTING_POLICY_SEAL_REQUIRED")
    else:
        seal = store.get_json(seal_path)
        require(seal == {"schema_version": "argon-routing-policy-seal.v1",
                         "routing_policy_sha256": sha(raw)}, "ARGON_ROUTING_POLICY_DRIFT")
    return {"scope": routing["scope"], "routing_policy_sha256": sha(raw),
            "routing_policy_version": routing["schema_version"]}


def native_binding(task, request_sha256, binary):
    return {"schema_version": SCHEMA, "kind": "agy", "task_id": task.name,
            **scope_binding(task), "requested_model": MODEL,
            "request_sha256": request_sha256, "selected_model_label": AGY_LABEL,
            "metadata_argv": [str(binary), "models"], "metadata_cwd": str(task)}


def native_predecessor(task):
    scope = scope_binding(task)
    if scope["scope"] != "production":
        return {"basis": "EXISTING_" + scope["scope"].upper()}
    from .argon_escalation_witness import ArgonEscalationWitness
    from . import argon_escalation_agy as native
    from . import agy_gemini_client as agy
    first = task.parents[2] / "attempt-1" / "engine" / task.name
    manifest = store.get_json(first / "manifest.json")
    require(sha(canonical(manifest)) == task.name
            and manifest == store.get_json(task / "manifest.json"), "CAPABILITY_PREDECESSOR_TASK_DRIFT")
    inner = store.get_blob(first, manifest["inner_request_sha256"])
    engine = ArgonEscalationWitness(first.parent, read_only=True)
    ledger = engine._ledger(first, manifest, "agy", native.body(inner))
    snapshot = engine._read_snapshot(ledger)
    event = snapshot["events"][-1]
    anchor = {"task_id": first.name, "request_id": snapshot["request_id"],
              "terminal_event_sha256": sha(canonical(event)), "reason": event["reason_code"]}
    if snapshot["state"] == "FAILED_PRE_DISPATCH":
        receipt = native.validate_capability(first, inner,
            expected_sha=event["metadata"].get("capability_receipt_sha256"))
        require(receipt["status"] == "UNSUPPORTED"
                and receipt["reason"] in {"AGY_MODEL_UNSUPPORTED", "AGY_BINARY_ABSENT"}
                and receipt["reason"] == event["reason_code"], "CAPABILITY_PREDECESSOR_INVALID")
        return {**anchor, "capability_receipt_sha256": receipt["receipt_sha256"]}
    require(snapshot["state"] == "FAILED"
            and event["reason_code"] in {"AGY_QUOTA_EXHAUSTED", "AGY_RATE_LIMITED"},
            "CAPABILITY_PREDECESSOR_NOT_DEFINITIVE")
    process_sha = event["metadata"]["process_receipt_sha256"]
    process = strict_json(store.get_blob(first, process_sha))
    require(process == store.get_json(first / "agy-run.json")
            and process["terminal"] == "COMPLETED_PROCESS"
            and type(process["returncode"]) is int and process["returncode"] != 0,
            "CAPABILITY_PREDECESSOR_PROCESS_INVALID")
    execution_sha = engine._dispatch_metadata(snapshot)["execution_request_sha256"]
    execution = strict_json(store.get_blob(first, execution_sha))
    require(process["execution_request_sha256"] == execution_sha
            and execution["requested_model"] == MODEL
            and execution["selected_model_label"] == AGY_LABEL
            and execution["request_sha256"] == sha(native.body(inner))
            and execution["input_audio_sha256"] == manifest["binding"]["input_audio_sha256"]
            and execution["argv"] == agy.agy_argv(execution["argv"][0],
                job_dir=first / "agy-input", model=AGY_LABEL, prompt=native.CLI_PROMPT,
                print_timeout=execution["print_timeout"]), "CAPABILITY_PREDECESSOR_EXECUTION_DRIFT")
    prompt = strict_json(native.body(inner))["prompt"].encode()
    require(store.read(first / "agy-input/prompt.md") == prompt
            and file_sha(first / "agy-input/input.wav") == manifest["binding"]["input_audio_sha256"],
            "CAPABILITY_PREDECESSOR_INPUT_DRIFT")
    if execution["schema_version"] == "argon-agy-execution.v2":
        require(native.validate_capability(first, inner,
            expected_sha=execution["capability_receipt_sha256"])["status"] == "SUPPORTED",
            "CAPABILITY_PREDECESSOR_MODEL_INVALID")
    else:
        require(execution["schema_version"] == "argon-agy-execution.v1",
                "CAPABILITY_PREDECESSOR_EXECUTION_INVALID")
    stdout = store.get_blob(first, process["stdout_sha256"]).decode()
    stderr = store.get_blob(first, process["stderr_sha256"]).decode()
    require(agy.classify_agy_failure(process["returncode"], stdout, stderr) == event["reason_code"],
            "CAPABILITY_PREDECESSOR_QUOTA_INVALID")
    return {**anchor, "process_receipt_sha256": process_sha,
            "execution_request_sha256": execution_sha}


def api_binding(task, request_sha256, mode, route, *, schema_version=API_SCHEMA, tier=None):
    require(mode in {"batch", "realtime"}, "CAPABILITY_MODE_INVALID")
    require(schema_version in {SCHEMA, API_SCHEMA}, "CAPABILITY_SCHEMA_INVALID")
    root = task.parents[2]
    ordinal = route["accepted_key_ordinal"]
    identity = store.get_json(root / ("credential-" + str(ordinal) + ".json"))
    binding = {"schema_version": schema_version, "kind": "api", "task_id": task.name,
            **scope_binding(task), "requested_model": MODEL,
            "request_sha256": request_sha256, "mode": mode,
            "method": "GET", "endpoint": API_ORIGIN + API_PATH,
            "required_method": "batchGenerateContent" if mode == "batch" else "generateContent",
            "credential_identity": identity["binding"],
            "native_predecessor": native_predecessor(task),
            "route": {k: route[k] for k in
                      ("configured_key_count", "accepted_key_ordinal", "accepted_key_tier")}}
    if schema_version == API_SCHEMA:
        # New receipts record the current table; a retained receipt replays
        # with the tier fact it recorded (its original evidence scope).
        binding["tier_inference"] = (dict(tier) if tier is not None
                                     else tier_inference(route["accepted_key_tier"]))
    return binding


def classify(binding, terminal, raw):
    """Only complete, structurally valid metadata proves support or absence."""
    if terminal.get("binary_absent") is True:
        require(binding["kind"] == "agy"
                and terminal.get("metadata_process_started") is False,
                "CAPABILITY_TERMINAL_INVALID")
        return "UNSUPPORTED", "AGY_BINARY_ABSENT"
    if terminal.get("complete") is not True:
        return "UNKNOWN", "ARGON_CAPABILITY_METADATA_UNKNOWN"
    if binding["kind"] == "agy":
        if terminal.get("returncode") != 0:
            return "UNKNOWN", "ARGON_AGY_CATALOG_FAILED"
        try:
            text = raw.decode("utf-8")
            lines = text.splitlines()
            require(bool(lines) and all(lines), "CATALOG_EMPTY")
            pairs = [line.split("\t") for line in lines]
            require(all(len(p) == 2 and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", p[0])
                        and p[1] and p[1] == p[1].strip()
                        and all(ord(c) >= 32 for c in p[1]) for p in pairs),
                    "CATALOG_MALFORMED")
            require(len({p[0] for p in pairs}) == len(pairs)
                    and len({p[1] for p in pairs}) == len(pairs), "CATALOG_AMBIGUOUS")
            matching = [p for p in pairs if p[0] == MODEL or p[1] == AGY_LABEL]
            if matching == [[MODEL, AGY_LABEL]]:
                return "SUPPORTED", "ARGON_AGY_MODEL_SUPPORTED"
            if matching:
                return "UNKNOWN", "ARGON_AGY_MODEL_MAPPING_AMBIGUOUS"
            return "UNSUPPORTED", "AGY_MODEL_UNSUPPORTED"
        except (ValueError, UnicodeError, TypeError):
            return "UNKNOWN", "ARGON_AGY_CATALOG_INVALID"
    if binding["schema_version"] == API_SCHEMA:
        tier = binding.get("tier_inference")
        require(isinstance(tier, dict) and set(tier) == {"tier", "status", "basis"}
                and tier["tier"] == binding["route"]["accepted_key_tier"]
                and tier["status"] in {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}
                and isinstance(tier["basis"], str), "CAPABILITY_TIER_BINDING_INVALID")
        if tier["status"] != "AVAILABLE":
            # Listed metadata is not an inference entitlement for this tier.
            return "UNSUPPORTED", "ARGON_API_TIER_UNAVAILABLE"
    status = terminal.get("http_status")
    try:
        payload = strict_json(raw)
    except (ValueError, UnicodeError):
        return "UNKNOWN", "ARGON_API_METADATA_INVALID"
    if status == 404 and isinstance(payload.get("error"), dict):
        error = payload["error"]
        if error.get("code") == 404 and error.get("status") == "NOT_FOUND":
            return "UNSUPPORTED", "ARGON_API_MODEL_UNSUPPORTED"
    if status != 200:
        # Metadata 429/403/5xx is not a generation quota receipt.
        return "UNKNOWN", "ARGON_API_METADATA_HTTP_" + str(status)
    if payload.get("name") != API_PATH:
        return "UNKNOWN", "ARGON_API_MODEL_IDENTITY_MISMATCH"
    methods = payload.get("supportedGenerationMethods")
    if (not isinstance(methods, list) or not methods
            or any(not isinstance(m, str) or not m for m in methods)
            or len(methods) != len(set(methods))):
        return "UNKNOWN", "ARGON_API_METHODS_INVALID"
    if binding["required_method"] not in methods:
        return "UNSUPPORTED", "ARGON_API_METHOD_UNSUPPORTED"
    return "SUPPORTED", "ARGON_API_MODEL_SUPPORTED"


def validate(task, name, binding, *, expected_sha=None):
    pointer = store.get_json(task / ("capability-" + name + ".json"))
    digest = pointer["receipt_sha256"]
    require(expected_sha is None or expected_sha == digest, "CAPABILITY_RECEIPT_DRIFT")
    receipt = strict_json(store.get_blob(task, digest))
    require(receipt["binding"] == binding
            and receipt["binding_sha256"] == sha(canonical(binding))
            and pointer["binding_sha256"] == receipt["binding_sha256"],
            "CAPABILITY_BINDING_DRIFT")
    require(store.get_json(task / ("capability-" + name + ".intent.json"))
            == {"schema_version": binding["schema_version"], "binding": binding,
                "inference_dispatch": "NOT_DISPATCHED", "audio_bytes_sent": 0},
            "CAPABILITY_INTENT_DRIFT")
    raw = store.get_blob(task, receipt["raw_sha256"])
    store.get_blob(task, receipt["stderr_sha256"])
    require(receipt["inference_dispatch"] == "NOT_DISPATCHED"
            and receipt["audio_bytes_sent"] == 0, "CAPABILITY_AUTHORITY_INVALID")
    status, reason = classify(binding, receipt["terminal"], raw)
    require((receipt["status"], receipt["reason"]) == (status, reason),
            "CAPABILITY_RAW_REPLAY_MISMATCH")
    return {**receipt, "receipt_sha256": digest}


def ensure(task, name, binding, probe):
    pointer = task / ("capability-" + name + ".json")
    intent = task / ("capability-" + name + ".intent.json")
    if os.path.lexists(pointer):
        return validate(task, name, binding)
    expected_intent = {"schema_version": binding["schema_version"], "binding": binding,
                       "inference_dispatch": "NOT_DISPATCHED", "audio_bytes_sent": 0}
    if os.path.lexists(intent):
        require(store.get_json(intent) == expected_intent, "CAPABILITY_BINDING_DRIFT")
        return {"status": "UNKNOWN", "reason": "ARGON_CAPABILITY_QUERY_OUTCOME_UNKNOWN",
                "binding": binding, "inference_dispatch": "NOT_DISPATCHED",
                "audio_bytes_sent": 0, "receipt_sha256": None}
    store.put_json(intent, expected_intent)
    started = time.time()
    terminal, raw, stderr = probe()
    require(isinstance(raw, bytes) and isinstance(stderr, bytes), "CAPABILITY_RAW_INVALID")
    status, reason = classify(binding, terminal, raw)
    receipt = {"schema_version": binding["schema_version"], "binding": binding,
               "binding_sha256": sha(canonical(binding)), "terminal": terminal,
               "raw_sha256": store.blob(task, raw),
               "stderr_sha256": store.blob(task, stderr),
               "status": status, "reason": reason, "started_unix_s": started,
               "ended_unix_s": time.time(), "inference_dispatch": "NOT_DISPATCHED",
               "audio_bytes_sent": 0}
    store.put_json(pointer, {"binding_sha256": receipt["binding_sha256"],
                           "receipt_sha256": store.blob(task, canonical(receipt))})
    return validate(task, name, binding)


def native_probe(binary, available, *, task, timeout_s, execute):
    if not available:
        try:
            os.stat(binary)
        except FileNotFoundError:
            return {"binary_absent": True, "metadata_process_started": False,
                    "complete": True}, b"", b""
        except OSError:
            pass
        return {"complete": False, "metadata_process_started": False,
                "reason": "BINARY_NOT_EXECUTABLE_OR_UNREADABLE"}, b"", b""
    try:
        result = execute([str(binary), "models"], cwd=str(task),
                         capture_output=True, timeout=min(30, timeout_s), umask=0o077)
        def as_bytes(value):
            return value.encode() if isinstance(value, str) else value or b""
        return {"complete": True, "metadata_process_started": True,
                "returncode": result.returncode}, as_bytes(result.stdout), as_bytes(result.stderr)
    except subprocess.TimeoutExpired as error:
        def prefix(value):
            return value.encode() if isinstance(value, str) else value or b""
        return {"complete": False, "metadata_process_started": True,
                "error_type": "TimeoutExpired"}, prefix(error.stdout), prefix(error.stderr)
    except OSError as error:
        return {"complete": False, "metadata_process_started": False,
                "error_type": type(error).__name__}, b"", b""


def api_probe(transport, timeout_s):
    try:
        result = transport.model_metadata(timeout_s=min(30, timeout_s))
        return {"complete": True, "metadata_request_started": True,
                "http_status": result.status}, result.body, b""
    except ProviderTransportError as error:
        return {"complete": False, "metadata_request_started": True,
                "http_status": error.http_status, "error_type": error.reason}, error.partial_response, b""
    except (OSError, ValueError, AttributeError) as error:
        return {"complete": False, "metadata_request_started": None,
                "error_type": type(error).__name__}, b"", b""


def validated_api_receipt(task, request_sha256, mode, route, *, expected_sha=None,
                          binding_task=None):
    # Reconstruct scope/credential/native predecessor at the original bound
    # acquisition, but validate intent/raw/receipt in the actual retained copy.
    binding_task = task if binding_task is None else binding_task
    require(binding_task.name == task.name, "CAPABILITY_BINDING_DRIFT")
    # A retained v1 receipt replays under its own (metadata-only) contract.
    pointer = store.get_json(task / ("capability-api-" + mode + ".json"))
    stored = strict_json(store.get_blob(task, pointer["receipt_sha256"]))
    version = stored.get("binding", {}).get("schema_version")
    require(version in {SCHEMA, API_SCHEMA}, "CAPABILITY_SCHEMA_INVALID")
    return validate(task, "api-" + mode,
                    api_binding(binding_task, request_sha256, mode, route, schema_version=version,
                                tier=stored["binding"].get("tier_inference") if version == API_SCHEMA else None),
                    expected_sha=expected_sha)
