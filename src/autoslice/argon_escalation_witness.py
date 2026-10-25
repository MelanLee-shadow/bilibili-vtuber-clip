"""Explicit, offline-testable Gemini Argon Batch escalation; not producer wiring.

Uses the research-proven v1beta batchGenerateContent/Operation/inline protocol
and the existing ContentBoundRequestLedger. The provider is injected: this
module never loads credentials, creates an HTTP session or retries a POST.
A timed-out batch remains queryable. Realtime fallback requires explicit opt-in
AND a confirmed failed/cancelled/expired remote job, never just elapsed time.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import math
from pathlib import Path
import re
import time
from typing import Callable, Protocol
from urllib.parse import urlencode
import uuid
import wave

from . import argon_escalation_storage as storage
from .argon_escalation_contract import (
    AudioSpec,
    ContractError,
    KeyPolicy,
    MAX_REQUEST_BYTES,
    MODEL,
    Pricing,
    SCHEMA,
    canonical,
    classify_provider_rejection,
    file_sha,
    make_inner_request,
    parse_hearing,
    parse_hearing_document,
    prepare_audio,
    require,
    sha,
    strict_json,
    usage_receipt,
    valid_sha,
    integer,
    window,
)
from .content_bound_request_ledger import ContentBoundRequestLedger
from .gemini_backup_policy import validate_key_acceptance_metadata, valid_paid_policy_stamp
from .gemini_backup_policy import CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE

_JOB = re.compile(r"batches/[A-Za-z0-9._~-]{1,512}")
_PENDING = {"BATCH_STATE_PENDING", "BATCH_STATE_RUNNING", "JOB_STATE_PENDING", "JOB_STATE_RUNNING"}
_SUCCESS = {"BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED"}
_FAILURE = {
    f"{prefix}_{state}"
    for prefix in ("BATCH_STATE", "JOB_STATE")
    for state in ("FAILED", "CANCELLED", "EXPIRED")
}
_FALLBACK_REASONS = {"REMOTE_TERMINAL_" + state for state in _FAILURE}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class ProviderTransportError(RuntimeError):
    """Incomplete transport observation; never a successful HTTP response.

    Adapters supply a bounded reason, actual status if headers arrived, and only
    bytes actually received. The caller persists this prefix as INCOMPLETE.
    """

    def __init__(
        self, reason: str, *, http_status: int | None = None, partial_response: bytes = b""
    ):
        require(
            reason
            in {
                "HTTP_TIMEOUT",
                "HTTP_CONNECTION_ERROR",
                "HTTP_RESPONSE_INCOMPLETE",
                "HTTP_RESPONSE_TOO_LARGE",
                "HTTP_FRAMING_INVALID",
            },
            "TRANSPORT_ERROR_REASON_INVALID",
        )
        require(
            http_status is None or type(http_status) is int and 100 <= http_status <= 599,
            "TRANSPORT_ERROR_STATUS_INVALID",
        )
        require(isinstance(partial_response, bytes), "TRANSPORT_ERROR_BYTES_INVALID")
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.partial_response = partial_response


class Provider(Protocol):
    """Authenticated transport supplied by the authorized caller.

    path is relative to https://generativelanguage.googleapis.com/v1beta/.
    Send body unchanged, exactly once; honor timeout_s; do not follow redirects
    or retry internally. route contains only policy metadata, NEVER key values.
    Return actual status/raw response bytes. Errors must not expose credentials.
    """

    def request(
        self, method: str, path: str, body: bytes | None, *, route: dict, timeout_s: float
    ) -> HttpResponse: ...


class ArgonEscalationWitness:
    def __init__(self, root: Path, *, read_only: bool = False):
        self.root = Path(root).expanduser().absolute()
        self.read_only = read_only
        (storage.existing_directory if read_only else storage.directory)(self.root)

    @staticmethod
    def _read_snapshot(ledger) -> dict:
        # Reuse the canonical ledger validator on its atomic file snapshot.
        # Read-only consumers must not recreate lock files or missing roots.
        return ledger._validate_record(storage.get_json(ledger.path))

    def read_status(self, task_id: str, *, expected: AudioSpec,
                    acquisition_root: Path | None = None) -> dict:
        """Read retained native state only: no prepare, HTTP, slot or transition."""
        task, manifest, inner = self._load(task_id, expected)
        mode = "batch"
        ledger = self._ledger(task, manifest, mode, self._body(task_id, inner, mode))
        snapshot = self._read_snapshot(ledger)
        realtime = self._ledger(task, manifest, "realtime", inner)
        if realtime.path.exists():
            candidate = self._read_snapshot(realtime)
            if self._dispatch_metadata(candidate):
                mode, ledger, snapshot = "realtime", realtime, candidate
        agy_ledger = self._ledger(task, manifest, "agy", self._body(task_id, inner, "agy"))
        if snapshot["state"] == "PREPARED" and agy_ledger.path.exists():
            mode, ledger, snapshot = "agy", agy_ledger, self._read_snapshot(agy_ledger)
        result = None
        result_sha = snapshot["events"][-1]["metadata"].get("result_sha256")
        if result_sha:
            result = self.validate_evidence(
                task_id, strict_json(storage.get_blob(task, result_sha)), expected=expected,
                acquisition_root=acquisition_root
            )
        return {
            "task_id": task_id,
            "mode": mode,
            "state": snapshot["state"],
            "job_name": self._job_name(snapshot),
            "request_id": snapshot["request_id"],
            "ledger_sha256": sha(storage.read(ledger.path)),
            "result": result,
            "result_sha256": result_sha,
        }

    def prepare(self, spec: AudioSpec) -> dict:
        """Freeze exact source/context and full inline request; zero provider use."""
        require(not self.read_only, "READ_ONLY_ENGINE")
        binding, audio = prepare_audio(spec)
        inner = make_inner_request(binding, audio)
        manifest = {
            "schema_version": SCHEMA,
            "binding": binding,
            "inner_request_sha256": sha(inner),
            "requested_model": MODEL,
        }
        task_id = sha(canonical(manifest))
        task = self.root / task_id
        with storage.locked(task):
            storage.blob(task, audio)
            storage.blob(task, inner)
            storage.put_json(task / "manifest.json", manifest)
            batch = self._body(task_id, inner, "batch")
            require(len(batch) < MAX_REQUEST_BYTES, "INLINE_BATCH_TOO_LARGE")
            storage.blob(task, batch)
            self._ledger(task, manifest, "batch", batch).prepare()
        return {
            "task_id": task_id,
            "binding": binding,
            "inner_request_sha256": sha(inner),
            "batch_request_sha256": sha(batch),
            "mutation_authorized": False,
            "release_authorized": False,
        }

    @staticmethod
    def _body(task_id: str, inner: bytes, mode: str) -> bytes:
        if mode == "realtime":
            return inner
        if mode == "agy":
            from .argon_escalation_agy import body
            return body(inner)
        return canonical(
            {
                "batch": {
                    "displayName": f"argon-{task_id}",
                    "inputConfig": {
                        "requests": {
                            "requests": [
                                {"request": strict_json(inner), "metadata": {"key": task_id}}
                            ]
                        }
                    },
                }
            }
        )

    @staticmethod
    def _ledger(task: Path, manifest: dict, mode: str, body: bytes):
        return ContentBoundRequestLedger(
            task / "ledgers",
            {
                "provider": "agy" if mode == "agy" else "gemini_api",
                "requested_model": MODEL,
                "mode": mode,
                "task_id": task.name,
                "request_sha256": sha(body),
                "input_audio_sha256": manifest["binding"]["input_audio_sha256"],
            },
        )

    def _load(self, task_id: str, expected: AudioSpec) -> tuple[Path, dict, bytes]:
        require(valid_sha(task_id), "TASK_ID_INVALID")
        task = self.root / task_id
        manifest_raw = storage.read(task / "manifest.json")
        manifest = strict_json(manifest_raw)
        require(
            sha(manifest_raw) == task_id
            and sha(canonical(manifest)) == task_id
            and manifest.get("schema_version") == SCHEMA
            and manifest.get("requested_model") == MODEL,
            "TASK_MANIFEST_MISMATCH",
        )
        binding = manifest["binding"]
        f = expected.finding.binding()
        window(expected.context_source_ms)
        require(
            integer(expected.source_origin_ms)
            and expected.source_origin_ms >= 0
            and integer(expected.minimum_context_ms)
            and expected.minimum_context_ms >= 400,
            "EXPECTED_CLOCK_INVALID",
        )
        require(
            all(binding.get(k) == v for k, v in f.items())
            and binding["context_source_ms"] == list(expected.context_source_ms)
            and binding["source_origin_ms"] == expected.source_origin_ms
            and binding["minimum_context_ms"] == expected.minimum_context_ms,
            "EXPECTED_SOURCE_WINDOW_FINDING_MISMATCH",
        )
        require(
            file_sha(Path(expected.source_path).expanduser().absolute())
            == binding["source_media_sha256"],
            "SOURCE_SHA_MISMATCH",
        )
        audio = storage.get_blob(task, binding["input_audio_sha256"])
        require(len(audio) == binding["input_audio_bytes"], "AUDIO_BYTES_MISMATCH")
        with wave.open(io.BytesIO(audio), "rb") as handle:
            require(
                (
                    handle.getframerate(),
                    handle.getnchannels(),
                    handle.getsampwidth(),
                    handle.getnframes(),
                )
                == (
                    binding["sample_rate"],
                    binding["channels"],
                    binding["sample_width"],
                    binding["sample_count"],
                ),
                "AUDIO_CLOCK_MISMATCH",
            )
            require(
                sha(handle.readframes(handle.getnframes())) == binding["pcm_sha256"],
                "AUDIO_PCM_MISMATCH",
            )
        inner = storage.get_blob(task, manifest["inner_request_sha256"])
        require(inner == make_inner_request(binding, audio), "REQUEST_AUDIO_BINDING_MISMATCH")
        return task, manifest, inner

    @staticmethod
    def _dispatch_metadata(snapshot: dict) -> dict:
        return next(
            (e["metadata"] for e in snapshot["events"] if e["state"] == "SUBMIT_DISPATCHED"), {}
        )

    @staticmethod
    def _job_name(snapshot: dict) -> str | None:
        names = {
            e["metadata"]["job_name"] for e in snapshot["events"] if e["metadata"].get("job_name")
        }
        require(len(names) <= 1, "REMOTE_JOB_IDENTITY_CONFLICT")
        return next(iter(names), None)

    def _call(
        self,
        task: Path,
        provider: Provider,
        method: str,
        path: str,
        body: bytes | None,
        route: dict,
        timeout_s: float,
        now: Callable,
    ) -> dict:
        """Persist intent BEFORE transport and raw response BEFORE outcome.

        A crash at any boundary leaves a reservation/attempt rather than a false
        zero-call claim. Read attempts are distinct from inference reservations.
        """
        call_id = uuid.uuid4().hex
        prefix = task / "calls" / call_id
        request_sha = storage.blob(task, body) if body is not None else None
        wire_body = body
        structured = {}
        if method == 'POST' and body is not None:
            from .argon_escalation_contract import structured_argon_request
            from .response_schemas import hearing_schema
            from .structured_output import contract_receipt, output_contract
            wire_body = structured_argon_request(body)
            structured = {'wire_request_sha256': storage.blob(task, wire_body),
                          'structured_output': contract_receipt(output_contract(hearing_schema()))}
        start = {
            "method": method,
            "path": path,
            "request_sha256": request_sha,
            "route": route,
            "started_unix_s": now(),
            **structured,
        }
        storage.put_json(prefix.with_suffix(".start.json"), start)
        try:
            response = provider.request(
                method, path, wire_body, route=strict_json(canonical(route)), timeout_s=timeout_s
            )
            require(
                isinstance(response, HttpResponse)
                and type(response.status) is int
                and 100 <= response.status <= 599
                and isinstance(response.body, bytes),
                "PROVIDER_TRANSPORT_CONTRACT_INVALID",
            )
        except ProviderTransportError as error:
            outcome = {
                "http_status": error.http_status,
                "response_sha256": storage.blob(task, error.partial_response)
                if error.http_status is not None or error.partial_response
                else None,
                "response_complete": False,
                "error_type": type(error).__name__,
                "transport_reason": error.reason,
                "finished_unix_s": now(),
            }
        except Exception as error:
            outcome = {
                "response_complete": False,
                "http_status": None,
                "response_sha256": None,
                "error_type": type(error).__name__,
                "finished_unix_s": now(),
            }
        else:
            digest = storage.blob(task, response.body)
            outcome = {
                "http_status": response.status,
                "response_sha256": digest,
                "response_complete": True,
                "error_type": None,
                "finished_unix_s": now(),
            }
        storage.put_json(prefix.with_suffix(".outcome.json"), outcome)
        return outcome

    def _operation_identity(
        self, raw: bytes, task_id: str, expected_name: str | None = None
    ) -> dict:
        op = strict_json(raw)
        name, metadata = op.get("name"), op.get("metadata")
        require(
            isinstance(name, str)
            and _JOB.fullmatch(name) is not None
            and (expected_name is None or name == expected_name),
            "REMOTE_JOB_IDENTITY_MISMATCH",
        )
        require(
            isinstance(metadata, dict)
            and metadata.get("model") == f"models/{MODEL}"
            and metadata.get("displayName") == f"argon-{task_id}"
            and metadata.get("name", name) == name,
            "REMOTE_JOB_CORRELATION_MISMATCH",
        )
        return op

    @staticmethod
    def _check_operation_result(op: dict) -> None:
        """One interpretation for every supported native result location.

        Identity can be retained independently of content validity. In particular,
        a contradictory result cannot qualify as a failed job for new inference.
        """
        metadata = op["metadata"]
        require("done" not in op or type(op["done"]) is bool, "REMOTE_DONE_INVALID")
        require(
            metadata.get("state") is None or isinstance(metadata.get("state"), str),
            "REMOTE_STATE_INVALID",
        )
        outputs = ([op["response"]] if "response" in op else []) + (
            [metadata["output"]] if "output" in metadata else []
        )
        has_error = "error" in op
        require(not (outputs and has_error), "REMOTE_RESULT_CONTRADICTION")
        for output in outputs:
            require(isinstance(output, dict), "REMOTE_OUTPUT_INVALID")
            require(
                not ("inlinedResponses" in output and "responsesFile" in output)
                and "error" not in output,
                "REMOTE_RESULT_CONTRADICTION",
            )
        require(
            not (
                any("inlinedResponses" in output for output in outputs)
                and any("responsesFile" in output for output in outputs)
            ),
            "REMOTE_RESULT_CONTRADICTION",
        )
        if has_error:
            error = op["error"]
            require(
                isinstance(error, dict)
                and integer(error.get("code"))
                and error["code"] > 0
                and ("message" not in error or isinstance(error["message"], str)),
                "REMOTE_ERROR_INVALID",
            )
        state, done = metadata.get("state"), op.get("done") is True
        require(
            not (outputs or has_error or state in _SUCCESS | _FAILURE) or done,
            "REMOTE_RESULT_NOT_COMPLETE",
        )
        require(not (done and state in _PENDING), "REMOTE_STATE_RESULT_CONTRADICTION")
        require(not outputs or state in _SUCCESS, "REMOTE_STATE_RESULT_CONTRADICTION")
        require(not has_error or state in _FAILURE, "REMOTE_STATE_RESULT_CONTRADICTION")

    def _operation(self, raw: bytes, task_id: str, expected_name: str | None = None) -> dict:
        op = self._operation_identity(raw, task_id, expected_name)
        self._check_operation_result(op)
        return op

    def _inline(self, op: dict, task_id: str) -> dict:
        versions = []
        for output in (op.get("response", {}), op.get("metadata", {}).get("output", {})):
            require(isinstance(output, dict), "REMOTE_OUTPUT_INVALID")
            rows = output.get("inlinedResponses")
            if isinstance(rows, dict):
                rows = rows.get("inlinedResponses")
            if rows is not None:
                require(isinstance(rows, list), "REMOTE_INLINE_INVALID")
                versions.append(rows)
        require(bool(versions), "REMOTE_INLINE_MISSING")
        require(all(v == versions[0] for v in versions), "REMOTE_OUTPUT_CONFLICT")
        rows = versions[0]
        require(
            len(rows) == 1
            and isinstance(rows[0], dict)
            and rows[0].get("metadata", {}).get("key") == task_id,
            "REMOTE_RESPONSE_KEY_MISMATCH",
        )
        row = rows[0]
        require(("response" in row) != ("error" in row), "REMOTE_ITEM_INCOMPLETE")
        require("error" not in row, "REMOTE_ITEM_ERROR")
        require(isinstance(row["response"], dict), "REMOTE_ITEM_INCOMPLETE")
        return row["response"]

    def _recover(
        self,
        task: Path,
        provider: Provider,
        route: dict,
        timeout_s: float,
        now: Callable,
        max_pages: int,
        ledger,
    ) -> tuple[dict | None, str]:
        # First recover a POST whose complete response was saved before a crash
        # in the outer state transition. No list or new inference is needed.
        found = {}
        for path in sorted((task / "calls").glob("*.start.json")):
            start = storage.get_json(path)
            end = path.with_name(path.name.replace(".start.json", ".outcome.json"))
            if start["method"] != "POST" or not path.name or not end.exists():
                continue
            outcome = storage.get_json(end)
            if (
                outcome["http_status"] == 200
                and outcome.get("response_complete", True)
                and start["path"].endswith(":batchGenerateContent")
            ):
                try:
                    raw = storage.get_blob(task, outcome["response_sha256"])
                    op = self._operation_identity(raw, task.name)
                except (ContractError, KeyError, TypeError):
                    continue
                found[op["name"]] = outcome
        if len(found) == 1:
            return next(iter(found.values())), "RECOVERED_LOCAL_SUBMIT_RESPONSE"
        require(not found, "MULTIPLE_LOCAL_SUBMISSIONS")
        # displayName is correlation, NOT a server-side idempotency key. Scan a
        # bounded complete list; partial pagination/duplicates cannot grant a retry.
        found, token, tokens = {}, "", set()
        for _ in range(max_pages):
            query = {"pageSize": 100}
            if token:
                query["pageToken"] = token
            outcome = self._call(
                task, provider, "GET", "batches?" + urlencode(query), None, route, timeout_s, now
            )
            if outcome["http_status"] != 200 or not outcome.get("response_complete", True):
                return None, "RECOVERY_READ_UNAVAILABLE"
            try:
                page = strict_json(storage.get_blob(task, outcome["response_sha256"]))
                operations = page.get("operations", [])
                require(
                    isinstance(operations, list) and not page.get("unreachable"),
                    "RECOVERY_LIST_INVALID",
                )
                for op in operations:
                    require(isinstance(op, dict), "RECOVERY_LIST_INVALID")
                    if op.get("metadata", {}).get("displayName") == f"argon-{task.name}":
                        valid = self._operation_identity(canonical(op), task.name)
                        found[valid["name"]] = (valid, outcome["response_sha256"])
                token = page.get("nextPageToken", "")
                require(isinstance(token, str), "RECOVERY_LIST_INVALID")
            except (ContractError, KeyError, TypeError, AttributeError):
                return None, "RECOVERY_LIST_INVALID"
            if not token:
                break
            if token in tokens:
                return None, "RECOVERY_PAGINATION_INCOMPLETE"
            tokens.add(token)
        else:
            return None, "RECOVERY_PAGINATION_INCOMPLETE"
        if len(found) != 1:
            return None, "RECOVERY_NO_MATCH" if not found else "RECOVERY_MULTIPLE_MATCHES"
        name = next(iter(found))
        # The list response remains raw in the call journal. Fetch the unique
        # resource, rather than reconstructing and pretending it was a raw GET.
        # Persist the unique recovered job BEFORE its GET, so a failed GET
        # cannot lose the recovered identity or silently switch to another job.
        ledger.transition(
            "PROCESSING",
            event="ARGON_REMOTE_ID_RECOVERED",
            metadata={"job_name": name, "recovery_list_response_sha256": found[name][1]},
        )
        return self._call(
            task, provider, "GET", name, None, route, timeout_s, now
        ), "RECOVERED_REMOTE_JOB"

    def _accounting(self, task: Path, manifest: dict, inner: bytes) -> dict:
        histories = []
        for mode in ("batch", "realtime", "agy"):
            ledger = self._ledger(task, manifest, mode, self._body(task.name, inner, mode))
            if not ledger.path.exists():
                continue
            snap = self._read_snapshot(ledger)
            histories.append(
                {
                    "mode": mode,
                    "ledger_path": str(ledger.path),
                    "ledger_sha256": sha(storage.read(ledger.path)),
                    "state": snap["state"],
                    "request_id": snap["request_id"],
                    "dispatch_reserved": bool(self._dispatch_metadata(snap)),
                    "job_name": self._job_name(snap),
                    "events": snap["events"],
                }
            )
        starts = list((task / "calls").glob("*.start.json"))
        return {
            "inference_reservations": sum(h["dispatch_reserved"] for h in histories),
            "http_attempts": len(starts),
            "agy_process_reservations": sum(h["dispatch_reserved"] for h in histories if h["mode"] == "agy"),
            "histories": histories,
            "reservation_is_confirmed_charge": False,
            "actual_charge_usd": None,
        }

    def _view(
        self,
        task: Path,
        manifest: dict,
        inner: bytes,
        ledger,
        mode: str,
        status: str,
        reason: str | None = None,
        **extra,
    ) -> dict:
        snapshot = ledger.snapshot()
        native_identity = {}
        if mode == "agy":
            from .argon_escalation_agy import AGY_MODEL
            native_identity = {"provider": "agy", "selected_model_label": AGY_MODEL,
                               "model_identity_basis": "ACTUAL_CLI_MODEL_SELECTION"}
        capability_digest = self._dispatch_metadata(snapshot).get("capability_receipt_sha256")
        return {
            **native_identity,
            **({"model_capability_receipt_sha256": capability_digest} if capability_digest else {}),
            "schema_version": SCHEMA,
            "task_id": task.name,
            "binding": manifest["binding"],
            "mode": mode,
            "status": status,
            "reason": reason,
            "requested_model": MODEL,
            "model_version": None,
            "response_sha256": None,
            "request_sha256": sha(self._body(task.name, inner, mode)),
            "inner_request_sha256": sha(inner),
            "job_name": self._job_name(snapshot),
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "release_authorized": False,
            "requires_cpa": True,
            "finding_resolved": False,
            "hearing": None,
            "usage_receipt": usage_receipt({}, mode, None),
            "accounting": self._accounting(task, manifest, inner),
            **extra,
        }

    def _decode(
        self, raw: bytes, task_id: str, binding: dict, mode: str, job_name: str | None
    ) -> tuple[dict, dict | None, str | None]:
        payload, heard, reason = {}, None, None
        try:
            if mode == "batch":
                op = self._operation(raw, task_id, job_name)
                require(
                    op.get("done") is True and op["metadata"].get("state") in _SUCCESS,
                    "REMOTE_RESULT_NOT_COMPLETE",
                )
                payload = self._inline(op, task_id)
            else:
                payload = strict_json(raw)
            heard = (parse_hearing_document(payload, binding) if mode == "agy"
                     else parse_hearing(payload, binding))
            require(
                not (binding["target_pcm_is_digital_silence"] and heard["transcript"] is not None),
                "DIGITAL_SILENCE_CONTRADICTS_TRANSCRIPT",
            )
        except (ContractError, KeyError, TypeError, AttributeError) as error:
            heard = None
            reason = error.reason if isinstance(error, ContractError) else "RESPONSE_SHAPE_INVALID"
        return payload, heard, reason

    def _finish(
        self, task: Path, manifest: dict, inner: bytes, ledger, mode: str, pricing: Pricing | None
    ) -> dict:
        snapshot = ledger.snapshot()
        digest = snapshot["events"][-1]["metadata"]["response_sha256"]
        raw = storage.get_blob(task, digest)
        payload, heard, reason = self._decode(
            raw, task.name, manifest["binding"], mode, self._job_name(snapshot)
        )
        usage = usage_receipt(payload, mode, pricing)
        result = self._view(
            task,
            manifest,
            inner,
            ledger,
            mode,
            heard["status"] if heard else "RESPONSE_REJECTED",
            reason,
            hearing=heard,
            response_sha256=digest,
            model_version=usage["model_version"],
            provider_payload_canonical_sha256=sha(canonical(payload)) if payload else None,
            usage_receipt=usage,
        )
        # The terminal object excludes a mutable accounting snapshot; public
        # readback projects current attempts from the original ledgers each time.
        result.pop("accounting")
        result_sha = storage.blob(task, canonical(result))
        ledger.transition(
            "COMPLETED" if heard else "FAILED",
            event="ARGON_RESULT_RETAINED",
            reason_code=reason,
            metadata={
                "result_sha256": result_sha,
                "response_sha256": digest,
                "model_version": usage["model_version"],
                "usage_receipt": usage,
            },
        )
        return {
            **result,
            "result_sha256": result_sha,
            "accounting": self._accounting(task, manifest, inner),
        }

    def _saved_result(self, task, manifest, inner, ledger, mode, expected) -> dict | None:
        snapshot = ledger.snapshot()
        digest = snapshot["events"][-1]["metadata"].get("result_sha256")
        if not digest:
            return None
        result = strict_json(storage.get_blob(task, digest))
        self.validate_evidence(task.name, result, expected=expected)
        return {
            **result,
            "result_sha256": digest,
            "accounting": self._accounting(task, manifest, inner),
        }

    def _acquisition_task(self, task, manifest, inner, mode, snapshot, acquisition_root):
        """Bind a retained copy to its original ledger, never discover by scanning.

        The ordinary consumer supplies its source-bound batch root. The
        runtime's own create-only route-refusal marker chain locates the lane
        (``storage.serving_lane``); AGY only ever runs in lane 1. The recorded
        route ordinal is a key ordinal, not a lane index (a tier-closed free
        chain or a refused free lane puts the paid ordinal in an earlier lane),
        so it is still validated but never used as a path. A changed storage
        path is not a new request or permission to repair/copy records.
        """
        if acquisition_root is None:
            return task  # Canonical runtime/standalone engine, not a relocated wrapper.
        require(self.read_only, "READ_ONLY_ENGINE")
        root = Path(acquisition_root).expanduser().absolute()
        storage.existing_directory(root)
        ordinal = (1 if mode == "agy" else
                   self._dispatch_metadata(snapshot)["route"]["accepted_key_ordinal"])
        require(type(ordinal) is int and 1 <= ordinal <= 4, "EVIDENCE_BINDING_MISMATCH")
        lane = 1 if mode == "agy" else storage.serving_lane(root)
        original = root / ("attempt-" + str(lane)) / "engine" / task.name
        storage.existing_directory(original)
        if original != task:
            require(storage.get_json(original / "manifest.json") == manifest
                    and storage.get_blob(original, manifest["inner_request_sha256"]) == inner,
                    "EVIDENCE_BINDING_MISMATCH")
            origin_engine = ArgonEscalationWitness(original.parent, read_only=True)
            original_ledger = origin_engine._ledger(original, manifest, mode,
                                                    self._body(task.name, inner, mode))
            # Includes the original request_id, exact dispatch and terminal seal.
            require(origin_engine._read_snapshot(original_ledger) == snapshot,
                    "EVIDENCE_BINDING_MISMATCH")
        return original

    def validate_evidence(self, task_id: str, evidence: dict, *, expected: AudioSpec,
                          acquisition_root: Path | None = None) -> dict:
        """Read-only public consumer: replay raw/geometry, not a status-only gate."""
        task, manifest, inner = self._load(task_id, expected)
        mode = evidence.get("mode")
        require(mode in {"batch", "realtime", "agy"}, "EVIDENCE_MODE_INVALID")
        require(
            evidence.get("schema_version") == SCHEMA
            and evidence.get("task_id") == task_id
            and evidence.get("binding") == manifest["binding"]
            and evidence.get("request_sha256") == sha(self._body(task_id, inner, mode))
            and evidence.get("inner_request_sha256") == sha(inner)
            and evidence.get("requested_model") == MODEL,
            "EVIDENCE_BINDING_MISMATCH",
        )
        require(
            evidence.get("authority") == "EVIDENCE_ONLY"
            and evidence.get("mutation_authorized") is False
            and evidence.get("release_authorized") is False
            and evidence.get("finding_resolved") is False
            and evidence.get("requires_cpa") is True,
            "EVIDENCE_AUTHORITY_INVALID",
        )
        ledger = self._ledger(task, manifest, mode, self._body(task_id, inner, mode))
        snapshot = self._read_snapshot(ledger)
        require(evidence.get("job_name") == self._job_name(snapshot), "EVIDENCE_JOB_MISMATCH")
        terminal = snapshot["events"][-1]["metadata"]
        sealed_sha = terminal.get("result_sha256")
        require(valid_sha(sealed_sha), "EVIDENCE_NOT_TERMINALLY_RETAINED")
        sealed = strict_json(storage.get_blob(task, sealed_sha))
        supplied_core = {
            k: v for k, v in evidence.items() if k not in {"result_sha256", "accounting"}
        }
        require(
            canonical(supplied_core) == canonical(sealed)
            and evidence.get("result_sha256", sealed_sha) == sealed_sha,
            "EVIDENCE_SEAL_MISMATCH",
        )
        binding_task = self._acquisition_task(task, manifest, inner, mode, snapshot,
                                              acquisition_root)
        if mode == "agy":
            from .argon_escalation_agy import AGY_MODEL, validate_execution
            require(evidence.get("provider") == "agy"
                    and evidence.get("selected_model_label") == AGY_MODEL
                    and evidence.get("model_identity_basis") == "ACTUAL_CLI_MODEL_SELECTION",
                    "ARGON_AGY_MODEL_SELECTION_DRIFT")
            validate_execution(self, task, manifest, inner, snapshot,
                               binding_task=binding_task)
        if mode in {"batch", "realtime"}:
            require(evidence.get("model_capability_receipt_sha256")
                    == self._dispatch_metadata(snapshot).get("capability_receipt_sha256"),
                    "CAPABILITY_RECEIPT_DRIFT")
            self._validate_capability(task, inner, mode, snapshot, binding_task=binding_task)
        raw = storage.get_blob(task, evidence["response_sha256"])
        payload, heard, reason = self._decode(
            raw, task_id, manifest["binding"], mode, self._job_name(snapshot)
        )
        require(
            evidence.get("hearing") == heard
            and evidence.get("reason") == reason
            and evidence.get("status") == (heard["status"] if heard else "RESPONSE_REJECTED")
            and evidence.get("model_version")
            == usage_receipt(payload, mode, None)["model_version"],
            "EVIDENCE_RAW_REPLAY_MISMATCH",
        )
        # Caller-supplied accounting is never authoritative; reconstruct it from
        # the content-bound ledgers rather than trusting a pasted cost field.
        return {
            **sealed,
            "result_sha256": sealed_sha,
            "accounting": self._accounting(task, manifest, inner),
        }

    def advance_agy(self, task_id, *, expected, request_timeout_s=60):
        from .argon_escalation_agy import advance
        return advance(self, task_id, expected=expected, request_timeout_s=request_timeout_s)

    def advance(
        self,
        task_id: str,
        *,
        expected: AudioSpec,
        provider: Provider,
        key_policy: KeyPolicy | None = None,
        allow_realtime_fallback: bool = False,
        pricing: Pricing | None = None,
        request_timeout_s: float = 60,
        max_batch_wait_s: float = 86400,
        max_recovery_pages: int = 3,
        now: Callable[[], float] = time.time,
    ) -> dict:
        """One bounded advance, no background worker and no implicit POST retry."""
        require(not self.read_only, "READ_ONLY_ENGINE")
        require(valid_sha(task_id), "TASK_ID_INVALID")
        require(type(allow_realtime_fallback) is bool, "FALLBACK_POLICY_INVALID")
        require(
            type(request_timeout_s) in (int, float)
            and math.isfinite(request_timeout_s)
            and 0 < request_timeout_s <= 600,
            "REQUEST_TIMEOUT_INVALID",
        )
        require(
            type(max_batch_wait_s) in (int, float)
            and math.isfinite(max_batch_wait_s)
            and max_batch_wait_s >= 0,
            "BATCH_WAIT_INVALID",
        )
        require(
            type(max_recovery_pages) is int and 1 <= max_recovery_pages <= 100,
            "RECOVERY_PAGE_LIMIT_INVALID",
        )
        with storage.locked(self.root / task_id):
            task, manifest, inner = self._load(task_id, expected)
            ledger = self._ledger(task, manifest, "batch", self._body(task_id, inner, "batch"))
            snapshot = ledger.snapshot()
            mode = "batch"
            fallback = (
                snapshot["state"] == "FAILED"
                and snapshot["events"][-1]["reason_code"] in _FALLBACK_REASONS
            )
            fallback_proof = None
            last = snapshot["events"][-1]
            # COST-01 / 41 Argon entry: a batch submit refused with a definitive
            # zero-inference FAILED_PRECONDITION may be a batch-only limit of
            # this route, so the same key's realtime method is the next route
            # (explicit realtime permission still required). 429/auth/body
            # errors never qualify; the ladder above owns the key change.
            submit_unservable = (
                snapshot["state"] == "FAILED"
                and last["reason_code"] == "SUBMIT_HTTP_REJECTED"
                and classify_provider_rejection(
                    last["metadata"].get("http_status"),
                    storage.get_blob(task, last["metadata"]["response_sha256"]),
                    response_complete=last["metadata"].get("response_complete", True),
                )["basis"] == "ZERO_INFERENCE_FAILED_PRECONDITION"
            )
            if submit_unservable:
                rt_ledger = self._ledger(task, manifest, "realtime", inner)
                rt_dispatched = rt_ledger.path.exists() and bool(
                    self._dispatch_metadata(rt_ledger.snapshot())
                )
                if rt_dispatched or allow_realtime_fallback:
                    fallback_proof = {
                        "explicit_realtime_permission": True,
                        "batch_request_id": snapshot["request_id"],
                        "batch_ledger_sha256": sha(storage.read(ledger.path)),
                        "batch_job_name": None,
                        "batch_terminal_response_sha256": last["metadata"]["response_sha256"],
                        "basis": "BATCH_SUBMIT_ZERO_INFERENCE_FAILED_PRECONDITION",
                    }
                    mode, ledger = "realtime", rt_ledger
                    if not ledger.path.exists():
                        ledger.prepare()
            if fallback:
                prior_meta = snapshot["events"][-1]["metadata"]
                prior_op = self._operation(
                    storage.get_blob(task, prior_meta["response_sha256"]),
                    task_id,
                    self._job_name(snapshot),
                )
                remote = prior_op["metadata"].get("state", "")
                # A batch authorization/quota denial is not permission to try a
                # different inference endpoint. Only known terminal transient
                # failures, expiry and cancellation are eligible here.
                eligible = remote.endswith(("_EXPIRED", "_CANCELLED")) or (
                    remote.endswith("_FAILED")
                    and prior_op.get("error", {}).get("code") in {4, 13, 14}
                )
                rt_ledger = self._ledger(task, manifest, "realtime", inner)
                rt_dispatched = rt_ledger.path.exists() and bool(
                    self._dispatch_metadata(rt_ledger.snapshot())
                )
                if rt_dispatched or (eligible and allow_realtime_fallback):
                    fallback_proof = {
                        "explicit_realtime_permission": True,
                        "batch_request_id": snapshot["request_id"],
                        "batch_ledger_sha256": sha(storage.read(ledger.path)),
                        "batch_job_name": self._job_name(snapshot),
                        "batch_terminal_response_sha256": prior_meta["response_sha256"],
                    }
                    mode, ledger = "realtime", rt_ledger
                    ledger.prepare()
            return self._advance_mode(
                task,
                manifest,
                inner,
                ledger,
                mode,
                expected,
                provider,
                key_policy,
                pricing,
                request_timeout_s,
                max_batch_wait_s,
                max_recovery_pages,
                now,
                fallback_proof,
            )

    def _validate_capability(self, task, inner, mode, snapshot, *, binding_task=None):
        binding_task = task if binding_task is None else binding_task
        meta = self._dispatch_metadata(snapshot)
        digest = meta.get("capability_receipt_sha256")
        has_capability = "capability_receipt_sha256" in meta
        artifacts = [task / ("capability-api-" + mode + suffix)
                     for suffix in (".json", ".intent.json")]
        has_artifact = any(p.exists() or p.is_symlink() for p in artifacts)
        from .argon_model_capability import scope_binding, validated_api_receipt
        scope = scope_binding(binding_task, allow_legacy_unsealed=(
            bool(meta) and not has_capability and not has_artifact))
        if meta and (has_capability or has_artifact or (
                scope.get("scope") == "production" and scope.get(
                    "routing_policy_version") == "argon-routing-policy.v2")):
            require(valid_sha(digest), "ARGON_API_CAPABILITY_REQUIRED")
        if has_capability:
            receipt = validated_api_receipt(task, sha(self._body(task.name, inner, mode)),
                mode, meta["route"], expected_sha=digest, binding_task=binding_task)
            require(receipt["status"] == "SUPPORTED", "ARGON_API_CAPABILITY_REQUIRED")

    def _advance_mode(
        self,
        task,
        manifest,
        inner,
        ledger,
        mode,
        expected,
        provider,
        policy,
        pricing,
        timeout_s,
        max_wait_s,
        max_pages,
        now,
        fallback_proof,
    ):
        snapshot = ledger.snapshot()
        self._validate_capability(task, inner, mode, snapshot)
        if snapshot["state"] in {"COMPLETED", "FAILED", "FAILED_PRE_DISPATCH"}:
            saved = self._saved_result(task, manifest, inner, ledger, mode, expected)
            return saved or self._view(
                task,
                manifest,
                inner,
                ledger,
                mode,
                "FAILED",
                snapshot["events"][-1]["reason_code"],
                response_sha256=snapshot["events"][-1]["metadata"].get("response_sha256"),
            )
        if snapshot["state"] == "RESULT_AVAILABLE":
            return self._finish(task, manifest, inner, ledger, mode, pricing)
        dispatched = self._dispatch_metadata(snapshot)
        body = self._body(task.name, inner, mode)
        outcome = None
        if snapshot["state"] == "PREPARED":
            if policy is None:
                return self._view(
                    task, manifest, inner, ledger, mode, "NOT_DISPATCHED", "KEY_POLICY_REQUIRED"
                )
            route = policy.select(task.name)
            capability_digest = (policy.capability_receipt(mode, route, sha(body))
                                 if hasattr(policy, "capability_receipt") else None)
            if route["accepted_key_tier"] == "paid_backup":
                stamp = route["paid_backup_policy"]
                for history in self._accounting(task, manifest, inner)["histories"]:
                    for event in history["events"]:
                        prior = event["metadata"].get("route", {}).get("paid_backup_policy")
                        require(
                            prior is None or canonical(prior) != canonical(stamp),
                            "PAID_STAMP_ALREADY_RESERVED",
                        )
            # Persist before crossing the side-effect boundary. A SIGKILL even
            # before the HTTP call is conservatively an unknown reservation.
            snapshot = ledger.transition(
                "SUBMIT_DISPATCHED",
                event="ARGON_DISPATCH_RESERVED",
                metadata={
                    "route": route,
                    "mode": mode,
                    "requested_model": MODEL,
                    "request_sha256": sha(body),
                    "dispatched_unix_s": now(),
                    "deadline_unix_s": now() + max_wait_s,
                    "estimated_usd": None,
                    "charge_status": "UNKNOWN",
                    "fallback_policy": fallback_proof,
                    **({"capability_receipt_sha256": capability_digest}
                       if capability_digest is not None else {}),
                },
            )
            dispatched = self._dispatch_metadata(snapshot)
            outcome = self._call(
                task,
                provider,
                "POST",
                f"models/{MODEL}:"
                + ("batchGenerateContent" if mode == "batch" else "generateContent"),
                body,
                route,
                timeout_s,
                now,
            )
            if outcome["http_status"] != 200 or not outcome.get("response_complete", True):
                # No POST is automatically retried, including deterministic HTTP
                # errors. 429/auth failures cannot become an alternate-route loop.
                ambiguous = not outcome.get("response_complete", True) or outcome[
                    "http_status"
                ] not in {
                    400,
                    401,
                    403,
                    404,
                    405,
                    413,
                    415,
                    422,
                    429,
                }
                ledger.transition(
                    "SUBMIT_AMBIGUOUS" if ambiguous else "FAILED",
                    event="ARGON_SUBMIT_OUTCOME",
                    reason_code="SUBMIT_OUTCOME_UNKNOWN" if ambiguous else "SUBMIT_HTTP_REJECTED",
                    metadata=outcome,
                )
                return self._view(
                    task,
                    manifest,
                    inner,
                    ledger,
                    mode,
                    "SUBMIT_UNKNOWN" if ambiguous else "FAILED",
                    "SUBMIT_OUTCOME_UNKNOWN" if ambiguous else "SUBMIT_HTTP_REJECTED",
                    response_sha256=outcome["response_sha256"],
                )
        route = dispatched["route"]
        require(validate_key_acceptance_metadata(**route, allow_free_chain_unservable=True) is None,
                "PERSISTED_KEY_POLICY_INVALID")
        if route["accepted_key_tier"] == "paid_backup":
            require(
                valid_paid_policy_stamp(
                    route["paid_backup_policy"],
                    expected_purpose=CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE,
                    expected_item_key=task.name,
                    allow_free_chain_unservable=True,
                ),
                "PERSISTED_PAID_POLICY_INVALID",
            )
        if mode == "realtime":
            if outcome is None:
                # Synchronous API has no remote retrieval ID. A saved exact POST
                # outcome is recoverable; otherwise preserve unknown, never resend.
                for path in sorted((task / "calls").glob("*.start.json")):
                    start = storage.get_json(path)
                    end = path.with_name(path.name.replace(".start.json", ".outcome.json"))
                    if start["path"] == f"models/{MODEL}:generateContent" and end.exists():
                        candidate = storage.get_json(end)
                        if candidate["http_status"] == 200 and candidate.get(
                            "response_complete", True
                        ):
                            require(outcome is None, "MULTIPLE_REALTIME_RESPONSES")
                            outcome = candidate
                if outcome is None:
                    return self._view(
                        task,
                        manifest,
                        inner,
                        ledger,
                        mode,
                        "SUBMIT_UNKNOWN",
                        "REALTIME_OUTCOME_UNKNOWN",
                    )
            ledger.transition("RESULT_AVAILABLE", event="ARGON_REALTIME_RESPONSE", metadata=outcome)
            return self._finish(task, manifest, inner, ledger, mode, pricing)
        name = self._job_name(ledger.snapshot())
        if outcome is None and name is None:
            outcome, recovery_reason = self._recover(
                task, provider, route, timeout_s, now, max_pages, ledger
            )
            name = self._job_name(ledger.snapshot())
            if (
                outcome is None
                or outcome["http_status"] != 200
                or not outcome.get("response_complete", True)
            ):
                return self._view(
                    task,
                    manifest,
                    inner,
                    ledger,
                    mode,
                    "READBACK_UNKNOWN" if name else "SUBMIT_UNKNOWN",
                    recovery_reason,
                )
        elif outcome is None:
            outcome = self._call(task, provider, "GET", name, None, route, timeout_s, now)
            if outcome["http_status"] != 200 or not outcome.get("response_complete", True):
                return self._view(
                    task,
                    manifest,
                    inner,
                    ledger,
                    mode,
                    "READBACK_UNKNOWN",
                    "BATCH_READBACK_UNAVAILABLE",
                    response_sha256=outcome["response_sha256"],
                )
        try:
            op = self._operation_identity(
                storage.get_blob(task, outcome["response_sha256"]), task.name, name
            )
        except (ContractError, KeyError, TypeError, AttributeError) as error:
            reason = error.reason if isinstance(error, ContractError) else "REMOTE_SHAPE_INVALID"
            return self._view(
                task,
                manifest,
                inner,
                ledger,
                mode,
                "READBACK_REJECTED",
                reason,
                response_sha256=outcome["response_sha256"],
            )
        name, remote = op["name"], op["metadata"].get("state")
        metadata = {**outcome, "job_name": name, "remote_state": remote}
        try:
            self._check_operation_result(op)
        except ContractError:
            # Preserve the original wire response and valid job identity in a
            # terminal rejected evidence artifact. _finish and the public
            # consumer re-run the same check; no fallback reason is issued.
            ledger.transition(
                "RESULT_AVAILABLE", event="ARGON_CONFLICTING_RESPONSE_RETAINED", metadata=metadata
            )
            return self._finish(task, manifest, inner, ledger, mode, pricing)
        if op.get("done") is True and remote in _FAILURE:
            ledger.transition(
                "FAILED",
                event="ARGON_REMOTE_TERMINAL",
                reason_code="REMOTE_TERMINAL_" + remote,
                metadata=metadata,
            )
            return self._view(
                task,
                manifest,
                inner,
                ledger,
                mode,
                "FAILED",
                "REMOTE_TERMINAL_" + remote,
                response_sha256=outcome["response_sha256"],
            )
        if op.get("done") is True or remote in _SUCCESS:
            ledger.transition("RESULT_AVAILABLE", event="ARGON_BATCH_RESPONSE", metadata=metadata)
            return self._finish(task, manifest, inner, ledger, mode, pricing)
        ledger.transition("PROCESSING", event="ARGON_REMOTE_JOB_RETAINED", metadata=metadata)
        overdue = now() >= dispatched["deadline_unix_s"]
        return self._view(
            task,
            manifest,
            inner,
            ledger,
            mode,
            "WAIT_TIMEOUT"
            if overdue
            else "PENDING"
            if remote in _PENDING
            else "REMOTE_STATE_UNKNOWN",
            "BATCH_STILL_QUERYABLE_NO_RESUBMIT"
            if overdue
            else None
            if remote in _PENDING
            else "REMOTE_STATE_UNKNOWN",
            response_sha256=outcome["response_sha256"],
        )

    def poll(
        self,
        task_id: str,
        *,
        max_polls: int = 1,
        interval_s: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        **kwargs,
    ) -> dict:
        """Finite polling in the caller's execution; never schedules future work."""
        require(type(max_polls) is int and 1 <= max_polls <= 100, "POLL_LIMIT_INVALID")
        require(
            type(interval_s) in (int, float) and math.isfinite(interval_s) and interval_s >= 0,
            "POLL_INTERVAL_INVALID",
        )
        result = None
        for index in range(max_polls):
            if index:
                sleep(interval_s)
            result = self.advance(task_id, **kwargs)
            if result["status"] != "PENDING":
                break
        return result
