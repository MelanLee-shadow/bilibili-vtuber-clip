"""Private completed text responses, before parsing; no review/QC authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from .llm_client import LlmConfig
from .qixi_transaction_core import (
    _runner_lock_open_flags,
    _safe_parent,
    _same_safe_runner_lock,
    create_staged_inode,
    install_staged_inode,
    stable_regular_snapshot,
)

SCHEMA = "normal-text-generation-checkpoint.v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def provider_identity(call: object) -> dict | None:
    """Explicit safe routing, plus the typed canonical transport endpoint."""
    routing = getattr(call, "cpa_cache_identity", None)
    if not isinstance(routing, Mapping):
        return None
    models = routing.get("models")
    if models is None and isinstance(routing.get("model"), str):
        models = [routing["model"]]
    effort = routing.get("effort") or routing.get("reasoning_effort")
    transport = routing.get("transport")
    if (
        not isinstance(transport, str)
        or not transport.strip()
        or not isinstance(effort, str)
        or not effort.strip()
        or not isinstance(models, (list, tuple))
        or not models
        or any(not isinstance(model, str) or not model.strip() for model in models)
    ):
        return None
    result = {"transport": transport, "models": list(models), "effort": effort}
    for key in ("provider", "api_mode", "api_base_sha256", "max_tokens", "temperature"):
        value = routing.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    runtime = getattr(call, "provider_runtime_binding", None)
    if isinstance(runtime, Mapping):
        result["runtime"] = {
            key: runtime[key]
            for key in (
                "provider_transport",
                "provider_endpoint_host",
                "provider_endpoint_path",
                "provider_endpoint_sha256",
                "provider_runtime_host",
                "provider_credential_source",
            )
            if isinstance(runtime.get(key), str)
        }
    # The existing canonical factory captures a typed LlmConfig. Read only
    # its endpoint, rather than serializing arbitrary closures or environments.
    # This binds scheme/port too, beyond the public runtime host/path receipt.
    if getattr(call, "__module__", None) == LlmConfig.__module__:
        for cell in getattr(call, "__closure__", None) or ():
            config = cell.cell_contents
            if isinstance(config, LlmConfig):
                environment = (
                    config.command_child_env if config.command_child_env is not None else os.environ
                )
                endpoint = (
                    config.api_base
                    if config.transport == "direct"
                    else environment.get("CPA_BASE_URL", "")
                )
                if endpoint:
                    result.setdefault("runtime", {})["provider_endpoint_sha256"] = _sha(
                        endpoint.strip().rstrip("/").encode()
                    )
                if transport == "cpa_command":
                    # This script frames the actual request (including its
                    # output-token limit), beyond prompt/model/effort alone.
                    scripts = [
                        Path(part)
                        for part in shlex.split(config.command_template or "")
                        if Path(part).name == "llm_via_cpa.sh"
                    ]
                    if len(scripts) != 1:
                        return None
                    snapshot = stable_regular_snapshot(
                        scripts[0].resolve(), label="text generation CPA framing"
                    )
                    if snapshot is None:
                        return None
                    result["request_framing_sha256"] = snapshot.sha256
    # A command's models/effort alone do not identify the endpoint it calls.
    if transport in {"cpa_command", "ssh_runtime_cpa"}:
        runtime = result.get("runtime") or {}
        if not any(
            runtime.get(key)
            for key in (
                "provider_endpoint_host",
                "provider_endpoint_sha256",
                "provider_runtime_host",
            )
        ):
            return None
    return json.loads(_canonical(result))


def subtitle_dependency(path: Path) -> dict:
    snapshot = stable_regular_snapshot(path.absolute(), label="text generation subtitle")
    if snapshot is None:
        raise ValueError("TEXT_CHECKPOINT_SUBTITLE_MISSING")
    return {"subtitle_sha256": snapshot.sha256}


def _atomic_json(path: Path, value: object) -> None:
    _safe_parent(path)
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("TEXT_CHECKPOINT_UNSAFE_FILE")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    create_staged_inode(temporary, payload=_canonical(value), mode=0o600, label="text checkpoint")
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path, expected_sha: object = None) -> tuple[dict, str]:
    snapshot = stable_regular_snapshot(path, label="text checkpoint")
    if snapshot is None:
        raise ValueError("TEXT_CHECKPOINT_MISSING")
    if expected_sha is not None and snapshot.sha256 != expected_sha:
        raise ValueError("TEXT_CHECKPOINT_RECEIPT_DRIFT")
    value = json.loads(snapshot.payload)
    if not isinstance(value, dict):
        raise ValueError("TEXT_CHECKPOINT_INVALID")
    return value, snapshot.sha256


def _read_or_staged_json(
    path: Path,
    *,
    validate: Callable[[dict], None],
    expected_sha: object = None,
) -> tuple[dict, str]:
    """Finish a matching fsynced staged inode, without replacing a foreign file."""
    temporaries = sorted(path.parent.glob("." + path.name + ".*.tmp"))
    if len(temporaries) > 1:
        raise ValueError("TEXT_CHECKPOINT_AMBIGUOUS_STAGED")
    existing = os.path.lexists(path)
    if not existing and not temporaries:
        raise ValueError("TEXT_CHECKPOINT_MISSING")
    if existing:
        value, sha = _read_json(path, expected_sha)
        validate(value)
        if temporaries and _read_json(temporaries[0])[1] != sha:
            raise ValueError("TEXT_CHECKPOINT_STAGED_DRIFT")
        return value, sha
    temporary = temporaries[0]
    if (
        re.fullmatch(re.escape("." + path.name + ".") + r"[0-9a-f]{32}\.tmp", temporary.name)
        is None
    ):
        raise ValueError("TEXT_CHECKPOINT_INVALID_STAGED")
    value, sha = _read_json(temporary, expected_sha)
    validate(value)
    snapshot = stable_regular_snapshot(temporary, label="text checkpoint staged original")
    if snapshot is None or snapshot.sha256 != sha:
        raise ValueError("TEXT_CHECKPOINT_STAGED_DRIFT")
    installed = install_staged_inode(
        snapshot,
        target=path,
        expected_before=None,
        label="text checkpoint staged recovery",
    )
    if installed.sha256 != sha:
        raise ValueError("TEXT_CHECKPOINT_STAGED_DRIFT")
    return value, sha


def completed_generation(
    *,
    artifact_root: Path,
    candidate_id: str,
    lane: str,
    attempt: int,
    prompt: str,
    call: Callable[[str], str],
    dependencies: Callable[[], Mapping],
    allow_dispatch: bool = True,
    _answer_provider: Mapping | None = None,
) -> str:
    """Serialize one input's dispatch and recover its complete original response.

    Transport failures have no reusable response. Complete malformed/rejected
    text *is* replayed to the existing parser/retry policy. No parser decision,
    fact review, final selection or PASS is cached here.
    """
    provider = dict(_answer_provider) if _answer_provider is not None else provider_identity(call)
    if lane == 'selection_rescore' and provider is not None:
        # This lane judges existing material. Titles/tags below are generation
        # and retain their independent attempt/deployment-bound observations.
        from .completed_review_checkpoint import CompletedReviewCheckpoint
        material = dict(dependencies())
        checkpoint = CompletedReviewCheckpoint.for_call(
            stage='selection-rescore', prompt=prompt, llm_call=call,
            inputs={'candidate_id': candidate_id, 'dependencies': material},
            parser_path=Path(__file__))
        if checkpoint is None:
            checkpoint = CompletedReviewCheckpoint.for_ruling(
                stage='selection-rescore', prompt=prompt, llm_call=call,
                inputs={'candidate_id': candidate_id, 'dependencies': material},
                parser_path=Path(__file__))
        if checkpoint is not None:
            if isinstance(attempt, bool) or attempt < 1:
                raise ValueError('TEXT_CHECKPOINT_IDENTITY_INVALID')
            if '..' in artifact_root.parts:
                raise ValueError('TEXT_CHECKPOINT_UNSAFE_DIRECTORY')
            if not allow_dispatch and checkpoint.load() is None:
                raise ValueError('TEXT_CHECKPOINT_NO_COMPLETE_RESPONSE')
            response = checkpoint.response(call)
            if dict(dependencies()) != material:
                raise ValueError('TEXT_CHECKPOINT_INPUT_DRIFT')
            return response
    if provider is None:
        if not allow_dispatch:
            raise ValueError('TEXT_CHECKPOINT_NO_COMPLETE_RESPONSE')
        return call(prompt)
    if lane not in {"title", "tags", "selection_rescore"} or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("TEXT_CHECKPOINT_IDENTITY_INVALID")

    def identity():
        return json.loads(
            _canonical(
                {
                    "schema_version": SCHEMA,
                    "lane": lane,
                    "attempt": attempt,
                    "prompt_sha256": _sha(prompt.encode()),
                    "provider": dict(_answer_provider) if _answer_provider is not None else provider_identity(call),
                    "dependencies": dict(dependencies()),
                }
            )
        )

    expected = identity()
    digest = hashlib.sha256(_canonical(expected)).hexdigest()
    candidate = hashlib.sha256(candidate_id.encode()).hexdigest()[:24]
    if ".." in artifact_root.parts:
        raise ValueError("TEXT_CHECKPOINT_UNSAFE_DIRECTORY")
    root = artifact_root.absolute() / ".normal-text-generations" / candidate / digest
    cursor = Path(root.anchor)
    for part in root.parts[1:]:
        cursor /= part
        try:
            cursor.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(cursor.lstat().st_mode):
            raise ValueError("TEXT_CHECKPOINT_UNSAFE_DIRECTORY")
    descriptor = os.open(root / "lock", _runner_lock_open_flags(), 0o600)
    try:
        opened = os.fstat(descriptor)
        _same_safe_runner_lock(root / "lock", descriptor, opened)
        # This input's local generation lock only; no runner/global lease.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _same_safe_runner_lock(root / "lock", descriptor, opened)
        if identity() != expected:
            raise ValueError("TEXT_CHECKPOINT_INPUT_DRIFT")
        pointer = root / "CURRENT.json"
        if os.path.lexists(pointer):
            locator, _ = _read_json(pointer)
            name = locator.get("receipt")
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[0-9a-f]{32}/result.json", name) is None
                or not isinstance(locator.get("sha256"), str)
            ):
                raise ValueError("TEXT_CHECKPOINT_INVALID")
            receipts = [(root / name, locator["sha256"])]
        else:
            # The completed response is durable before CURRENT is published.
            paths = set(root.glob("*/result.json"))
            paths.update(path.parent / "result.json" for path in root.glob("*/.result.json.*.tmp"))
            receipts = [(path, None) for path in sorted(paths)]
        valid = []
        for receipt, expected_sha in receipts:
            if re.fullmatch(r"[0-9a-f]{32}", receipt.parent.name) is None:
                raise ValueError("TEXT_CHECKPOINT_INVALID")

            def validate_request(request):
                if request != {
                    "schema_version": SCHEMA,
                    "identity": expected,
                    "request_text": prompt,
                }:
                    raise ValueError("TEXT_CHECKPOINT_ORIGINAL_DRIFT")

            def validate_result(saved):
                if saved.get("schema_version") != SCHEMA or saved.get("identity") != expected:
                    raise ValueError("TEXT_CHECKPOINT_IDENTITY_DRIFT")
                response = saved.get("response_text")
                if (
                    saved.get("status") not in {"COMPLETED", "INPUT_DRIFT_NOT_REUSABLE"}
                    or not isinstance(response, str)
                    or saved.get("response_sha256") != _sha(response.encode())
                    or not isinstance(saved.get("request_receipt_sha256"), str)
                ):
                    raise ValueError("TEXT_CHECKPOINT_ORIGINAL_DRIFT")
                _read_or_staged_json(
                    receipt.parent / "request.json",
                    validate=validate_request,
                    expected_sha=saved["request_receipt_sha256"],
                )

            saved, sha = _read_or_staged_json(
                receipt, validate=validate_result, expected_sha=expected_sha
            )
            if saved["status"] == "INPUT_DRIFT_NOT_REUSABLE":
                continue
            response = saved["response_text"]
            valid.append((receipt, sha, response))
        if len(valid) > 1:
            raise ValueError("TEXT_CHECKPOINT_AMBIGUOUS")
        if valid:
            receipt, sha, response = valid[0]
            if identity() != expected or _read_json(receipt)[1] != sha:
                raise ValueError("TEXT_CHECKPOINT_INPUT_DRIFT")
            if not pointer.exists():
                _atomic_json(pointer, {"receipt": str(receipt.relative_to(root)), "sha256": sha})
            from .cpa_runtime import record_cpa_event
            record_cpa_event('cache_hit',{'kind':'text_generation','stage':lane})
            from .llm_client import cached_completion
            return cached_completion(response, expected['provider'])
        if not allow_dispatch:
            raise ValueError('TEXT_CHECKPOINT_NO_COMPLETE_RESPONSE')
        directory = root / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        request_path = directory / "request.json"
        _atomic_json(
            request_path, {"schema_version": SCHEMA, "identity": expected, "request_text": prompt}
        )
        request_sha = _read_json(request_path)[1]
        try:
            response = call(prompt)
        except Exception as exc:
            _atomic_json(
                directory / "failure.json",
                {
                    "schema_version": SCHEMA,
                    "identity": expected,
                    "status": "NO_COMPLETE_RESPONSE",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        if not isinstance(response, str):
            raise ValueError("TEXT_CHECKPOINT_RESPONSE_NOT_TEXT")
        try:
            unchanged = identity() == expected
        except Exception:
            # Preserve the complete response even if an input disappeared.
            unchanged = False
        receipt = directory / "result.json"
        from .llm_client import completion_identity
        answering_provider = completion_identity(expected['provider'], response)
        fallback = answering_provider != expected['provider']
        _atomic_json(
            receipt,
            {
                "schema_version": SCHEMA,
                "identity": expected,
                "status": "COMPLETED" if unchanged and not fallback else "INPUT_DRIFT_NOT_REUSABLE",
                "answering_provider": answering_provider,
                "request_receipt_sha256": request_sha,
                "response_text": response,
                "response_sha256": _sha(response.encode()),
            },
        )
        if not unchanged or identity() != expected:
            raise ValueError("TEXT_CHECKPOINT_INPUT_DRIFT")
        if fallback:
            return completed_generation(artifact_root=artifact_root, candidate_id=candidate_id,
                lane=lane, attempt=attempt, prompt=prompt, call=lambda _: response,
                dependencies=dependencies, _answer_provider=answering_provider)
        _atomic_json(
            pointer, {"receipt": str(receipt.relative_to(root)), "sha256": _read_json(receipt)[1]}
        )
        return response
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
