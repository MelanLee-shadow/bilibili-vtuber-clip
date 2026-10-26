"""Native AGY adapter for the existing Argon engine and content-bound ledger.

No Gemini API response is synthesized from CLI text. Requested CLI selection,
actual process receipt, native hearing JSON and unknown cost remain separate.
"""
from pathlib import Path
import math
import os
import subprocess
import time

from . import agy_gemini_client as agy
from . import argon_escalation_storage as store
from . import argon_model_capability as capability
from .argon_escalation_contract import (
    MODEL, canonical, file_sha, require, sha, strict_json, valid_sha,
)

AGY_MODEL = capability.AGY_LABEL
SCHEMA = "argon-agy-request.v1"
CLI_PROMPT = (
    "Read prompt.md and listen to the actual input.wav audio attachment in this directory. "
    "Follow its candidate-blind hearing schema. Write only that complete JSON document "
    "to hearing.json. Do not use any other file or infer an answer from a filename."
)


def body(inner):
    request = strict_json(inner)
    prompt = request["contents"][0]["parts"][1]["text"]
    return canonical({"schema_version": SCHEMA, "selected_model_label": AGY_MODEL,
        "requested_model": MODEL, "inner_request_sha256": sha(inner),
        "audio_filename": "input.wav", "prompt_filename": "prompt.md",
        "response_filename": "hearing.json", "prompt": prompt, "cli_prompt": CLI_PROMPT})


def availability():
    binary = agy.resolve_local_agy_executable()
    return binary, agy.local_agy_available(binary, require_executable=True)


def execute_command(argv, **kwargs):
    """The actual local subprocess boundary; tests intercept only this function."""
    return subprocess.run(argv, **kwargs)


def execute_model_catalog(argv, **kwargs):
    """Metadata-only process boundary; never receives an audio or prompt input."""
    return subprocess.run(argv, **kwargs)


def validate_capability(task, inner, *, expected_sha=None, binding_task=None):
    binding_task = task if binding_task is None else binding_task
    require(binding_task.name == task.name, "CAPABILITY_BINDING_DRIFT")
    pointer = store.get_json(task / "capability-agy.json")
    receipt = strict_json(store.get_blob(task, pointer["receipt_sha256"]))
    binary = receipt["binding"]["metadata_argv"][0]
    binding = capability.native_binding(binding_task, sha(body(inner)), binary)
    return capability.validate(task, "agy", binding, expected_sha=expected_sha)


def validate_execution(engine, task, manifest, inner, snapshot, *, binding_task=None):
    binding_task = task if binding_task is None else binding_task
    meta = engine._dispatch_metadata(snapshot)
    require(isinstance(meta, dict) and valid_sha(meta.get("execution_request_sha256")),
            "ARGON_AGY_EXECUTION_REQUIRED")
    submitted = strict_json(store.get_blob(task, meta["execution_request_sha256"]))
    expected_body = body(inner)
    require(submitted["schema_version"] in {"argon-agy-execution.v1", "argon-agy-execution.v2"}
            and submitted["request_sha256"] == sha(expected_body)
            and submitted["input_audio_sha256"] == manifest["binding"]["input_audio_sha256"]
            and submitted["selected_model_label"] == AGY_MODEL,
            "ARGON_AGY_REQUEST_DRIFT")
    if submitted["schema_version"] == "argon-agy-execution.v2":
        receipt = validate_capability(task, inner,
            expected_sha=submitted["capability_receipt_sha256"], binding_task=binding_task)
        require(receipt["status"] == "SUPPORTED", "ARGON_AGY_CAPABILITY_REQUIRED")
    # v1 is retained historical CLI-selection evidence, not a new capability claim.
    argv = submitted["argv"]
    # argv/cwd describe the original execution, not this copy's storage path.
    # The current copy's actual prompt/audio/run/raw are still checked below.
    require(isinstance(argv, list)
            and submitted["cwd"] == str(binding_task / "agy-input")
            and argv == agy.agy_argv(argv[0], job_dir=binding_task / "agy-input",
                model=AGY_MODEL, prompt=CLI_PROMPT,
                print_timeout=submitted["print_timeout"]),
            "ARGON_AGY_MODEL_SELECTION_DRIFT")
    native = strict_json(expected_body)
    job = task / "agy-input"
    require(store.read(job / "prompt.md") == native["prompt"].encode()
            and file_sha(job / "input.wav") == manifest["binding"]["input_audio_sha256"],
            "ARGON_AGY_INPUT_DRIFT")
    run = store.get_json(task / "agy-run.json")
    require(run["execution_request_sha256"] == meta["execution_request_sha256"]
            and run["terminal"] == "COMPLETED_PROCESS" and run["returncode"] == 0
            and run["response_sha256"] == snapshot["events"][-1]["metadata"]["response_sha256"],
            "ARGON_AGY_TERMINAL_DRIFT")
    require(run.get("response_source") in {"hearing.json", "stdout"}
            and (run["response_source"] != "stdout"
                 or run["response_sha256"] == run["stdout_sha256"]),
            "ARGON_AGY_RESPONSE_SOURCE_DRIFT")
    for key in ("stdout_sha256", "stderr_sha256", "response_sha256"):
        store.get_blob(task, run[key])
    return run


def advance(engine, task_id, *, expected, request_timeout_s=60):
    if agy.cpa_gemini_client.enabled():
        agy.cpa_gemini_client.require_operation('argon_attested_agy_lifecycle')
    require(not engine.read_only, "READ_ONLY_ENGINE")
    require(valid_sha(task_id), "TASK_ID_INVALID")
    require(type(request_timeout_s) in (int, float) and math.isfinite(request_timeout_s)
            and 0 < request_timeout_s <= 600, "REQUEST_TIMEOUT_INVALID")
    with store.locked(engine.root / task_id):
        task, manifest, inner = engine._load(task_id, expected)
        native_body = body(inner)
        ledger = engine._ledger(task, manifest, "agy", native_body)
        ledger.prepare()
        snapshot = ledger.snapshot()
        saved = engine._saved_result(task, manifest, inner, ledger, "agy", expected)
        if saved is not None:
            return saved
        state = snapshot["state"]
        if state == "RESULT_AVAILABLE":
            validate_execution(engine, task, manifest, inner, snapshot)
            return engine._finish(task, manifest, inner, ledger, "agy", None)
        if state in {"SUBMIT_DISPATCHED", "SUBMIT_AMBIGUOUS"}:
            # No reliable remote ID exists for this CLI call. Never resubmit it
            # or silently move to API after a lost/ambiguous terminal.
            return engine._view(task, manifest, inner, ledger, "agy", "UNKNOWN",
                                "ARGON_AGY_DISPATCH_OUTCOME_UNKNOWN")
        if state != "PREPARED":
            capability_sha = snapshot["events"][-1]["metadata"].get("capability_receipt_sha256")
            if capability_sha:
                receipt = validate_capability(task, inner, expected_sha=capability_sha)
                require(receipt["status"] == "UNSUPPORTED"
                        and receipt["reason"] == snapshot["events"][-1]["reason_code"],
                        "ARGON_AGY_CAPABILITY_TERMINAL_DRIFT")
            return engine._view(task, manifest, inner, ledger, "agy", state,
                                snapshot["events"][-1]["reason_code"])

        binary, available = availability()
        admission = capability.ensure(task, "agy",
            capability.native_binding(task, sha(native_body), binary),
            lambda: capability.native_probe(binary, available, task=task,
                timeout_s=request_timeout_s, execute=execute_model_catalog))
        if admission["status"] == "UNKNOWN":
            return {**engine._view(task, manifest, inner, ledger, "agy",
                                   "CAPABILITY_UNKNOWN", admission["reason"]),
                    "capability": admission}
        if admission["status"] == "UNSUPPORTED":
            ledger.transition("FAILED_PRE_DISPATCH", event="ARGON_AGY_CAPABILITY_UNSUPPORTED",
                reason_code=admission["reason"],
                metadata={"binary": str(binary), "inference_process_started": False,
                          "capability_receipt_sha256": admission["receipt_sha256"]})
            return {**engine._view(task, manifest, inner, ledger, "agy",
                                  "FAILED_PRE_DISPATCH", admission["reason"]),
                    "capability": admission}
        job = task / "agy-input"
        store.directory(job)
        # A PREPARED request has not sent. Never accept a leftover output as
        # the response to a new successful process that wrote nothing.
        require(not os.path.lexists(job / "hearing.json"), "ARGON_AGY_PREEXISTING_RESPONSE")
        native = strict_json(native_body)
        store.immutable(job / "prompt.md", native["prompt"].encode())
        store.immutable(job / "input.wav", store.get_blob(task, manifest["binding"]["input_audio_sha256"]))
        print_timeout = str(max(1, int(request_timeout_s))) + "s"
        argv = agy.agy_argv(binary, job_dir=job, model=AGY_MODEL,
                            prompt=CLI_PROMPT, print_timeout=print_timeout)
        submitted = {"schema_version": "argon-agy-execution.v2", "argv": argv,
            "capability_receipt_sha256": admission["receipt_sha256"],
            "cwd": str(job), "selected_model_label": AGY_MODEL, "requested_model": MODEL,
            "request_sha256": sha(native_body), "input_audio_sha256": manifest["binding"]["input_audio_sha256"],
            "prompt_sha256": sha(native["prompt"].encode()), "print_timeout": print_timeout}
        submitted_sha = store.blob(task, canonical(submitted))
        dispatched = False

        def send(command, **kwargs):
            nonlocal dispatched
            require(command == argv and kwargs["cwd"] == str(job), "ARGON_AGY_TRANSPORT_DRIFT")
            require(file_sha(job / "input.wav") == manifest["binding"]["input_audio_sha256"]
                    and store.read(job / "prompt.md") == native["prompt"].encode(),
                    "ARGON_AGY_INPUT_DRIFT")
            require(not os.path.lexists(job / "hearing.json"), "ARGON_AGY_PREEXISTING_RESPONSE")
            require(validate_capability(task, inner,
                expected_sha=admission["receipt_sha256"])["status"] == "SUPPORTED",
                "ARGON_AGY_CAPABILITY_REQUIRED")
            # Common AGY client acquires the normal shared slot before this
            # actual process boundary. A slot refusal remains PREPARED.
            ledger.transition("SUBMIT_DISPATCHED", event="ARGON_AGY_SEND_RESERVED",
                metadata={"execution_request_sha256": submitted_sha,
                          "selected_model_label": AGY_MODEL, "started_at_s": time.time()})
            dispatched = True
            return execute_command(command, **kwargs, umask=0o077)

        run = agy.run_local_agy(argv, cwd=job, timeout=request_timeout_s, command_runner=send)
        if not dispatched:
            require(run.completed is None, "ARGON_AGY_UNRESERVED_RESULT")
            return engine._view(task, manifest, inner, ledger, "agy", "ADMISSION_PENDING",
                "ARGON_AGY_NOT_DISPATCHED_" + str(run.failure_category or "UNKNOWN"))
        stdout, stderr = (run.stdout, run.stderr) if run.completed is not None else run.partial_output()
        receipt = {"schema_version": "argon-agy-process-receipt.v1",
            "execution_request_sha256": submitted_sha,
            "terminal": "COMPLETED_PROCESS" if run.completed is not None else "UNKNOWN",
            "returncode": run.returncode, "failure_category": run.failure_category,
            "launch_error_type": run.launch_error_type,
            "stdout_sha256": store.blob(task, stdout.encode()),
            "stderr_sha256": store.blob(task, stderr.encode())}
        if run.completed is None:
            store.put_json(task / "agy-run.json", receipt)
            ledger.transition("SUBMIT_AMBIGUOUS", event="ARGON_AGY_TERMINAL_UNKNOWN",
                reason_code="ARGON_AGY_DISPATCH_OUTCOME_UNKNOWN",
                metadata={"process_receipt_sha256": store.blob(task, canonical(receipt))})
            return engine._view(task, manifest, inner, ledger, "agy", "UNKNOWN",
                                "ARGON_AGY_DISPATCH_OUTCOME_UNKNOWN")
        if run.returncode != 0:
            reason = agy.classify_agy_failure(run.returncode, stdout, stderr)
            # Complete explicit quota rejection permits the next route. A
            # nonzero process exit alone does not prove no remote work exists.
            rejected = reason in {agy.AGY_QUOTA_EXHAUSTED, "AGY_RATE_LIMITED"}
            store.put_json(task / "agy-run.json", receipt)
            ledger.transition("FAILED" if rejected else "SUBMIT_AMBIGUOUS",
                event="ARGON_AGY_PROCESS_REJECTED" if rejected else "ARGON_AGY_PROCESS_UNKNOWN",
                reason_code=reason,
                metadata={"process_receipt_sha256": store.blob(task, canonical(receipt))})
            return engine._view(task, manifest, inner, ledger, "agy",
                                "FAILED" if rejected else "UNKNOWN", reason)
        from_file = os.path.lexists(job / "hearing.json")
        response = store.read(job / "hearing.json") if from_file else stdout.encode()
        receipt["response_source"] = "hearing.json" if from_file else "stdout"
        receipt["response_sha256"] = store.blob(task, response)
        store.put_json(task / "agy-run.json", receipt)
        ledger.transition("RESULT_AVAILABLE", event="ARGON_AGY_RAW_RETAINED",
            metadata={"response_sha256": receipt["response_sha256"],
                      "process_receipt_sha256": store.blob(task, canonical(receipt))})
        validate_execution(engine, task, manifest, inner, ledger.snapshot())
        return engine._finish(task, manifest, inner, ledger, "agy", None)
