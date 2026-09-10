"""Private diagnostics for the existing aggregate pronoun pass, never a cache.

Preserve the actual prompt, each returned completion, and before/after SRT in a
separate owner-only run file. The unchanged pronoun callable still owns parsing,
retry count, edits and errors; neither this trace nor a hash is release approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable
import uuid

from src.autoslice.transcription_stage_cache import _directory, _write

SCHEMA = "aggregate-pronoun-stage-trace.v1"
_TEXT_LIMIT = 256_000
_LOG = logging.getLogger(__name__)


def _retained_text(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"retention": "NON_TEXT", "type": type(value).__name__}
    # Transport errors are never retained verbatim. A provider credential echo
    # is also omitted BEFORE hashing, so diagnostics cannot become a key store.
    credential = os.environ.get("CPA_API_KEY")
    if credential and credential in value:
        return {"retention": "OMITTED_CREDENTIAL_ECHO"}
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        return {"retention": "OMITTED_INVALID_UNICODE"}
    result = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    if len(raw) > _TEXT_LIMIT:
        return {**result, "retention": "OMITTED_SIZE"}
    return {**result, "retention": "RETAINED", "text": value}


def _model_identity(call: Callable) -> dict[str, Any]:
    declared = getattr(call, "cpa_cache_identity", None)
    if not isinstance(declared, dict):
        return {"status": "UNKNOWN"}
    # Do not copy command templates, child environments, endpoint URLs or keys.
    identity = {key: declared[key] for key in ("transport", "models", "effort") if key in declared}
    credential = os.environ.get("CPA_API_KEY")
    if credential and credential in str(identity):
        return {"status": "OMITTED_CREDENTIAL_ECHO"}
    if not (
        isinstance(identity.get("transport"), str)
        and isinstance(identity.get("models"), list)
        and all(isinstance(model, str) for model in identity["models"])
        and isinstance(identity.get("effort"), str)
    ):
        return {"status": "UNKNOWN"}
    return identity


def trace_pronoun_pass(
    srt: str, *, media_path: Path, llm_call: Callable[[str], str],
    run: Callable[..., str], required: bool, context_text: str = "",
) -> str:
    """Execute once, checkpoint its own calls, and return/raise exactly as before.

    Checkpoints only replace this invocation's newly created trace. Prior runs
    stay immutable. This is optional observability: an unsafe/unwritable sink is
    disclosed, not turned into a subtitle quality failure or a reason to retry.
    """
    started = time.monotonic()
    trace: dict[str, Any] = {
        "schema_version": SCHEMA,
        "trace_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "AGGREGATE_PRONOUN_TEXT_PASS_DIAGNOSTIC",
        "outcome": "RUNNING",
        "required": required,
        "model_identity": _model_identity(llm_call),
        "model_identity_source": "CALLABLE_DECLARED_REQUEST_CONFIGURATION",
        "backend_identity_verified": False,
        "input_srt": _retained_text(srt),
        "calls": [],
        "served_from_cache": False,
        "release_authorized": False,
    }
    path: Path | None = None
    warned = False

    def unavailable() -> None:
        nonlocal warned, path
        path = None
        if not warned:
            _LOG.warning("PRONOUN_STAGE_TRACE_UNAVAILABLE: diagnostic sink was not safely persisted")
            warned = True

    try:
        folder = Path(media_path).absolute().with_suffix(".pronoun-trace")
        _directory(folder)
        if folder.lstat().st_mode & 0o077:
            raise OSError("diagnostic directory must be private")
        candidate = folder / (trace["trace_id"] + ".json")
        if _write(candidate, trace):
            path = candidate
        else:
            unavailable()
    except (OSError, ValueError, TypeError):
        unavailable()

    def checkpoint() -> None:
        if path is None:
            return
        try:
            persisted = _write(path, trace, replace=True)
        except (OSError, ValueError, TypeError):
            persisted = False
        if not persisted:
            unavailable()

    def observed_call(prompt: str) -> str:
        call_started = time.monotonic()
        row = {"logical_call": len(trace["calls"]) + 1,
               "outcome": "STARTED", "prompt": _retained_text(prompt)}
        trace["calls"].append(row)
        checkpoint()  # Successful checkpointing preserves intent before dispatch.
        try:
            completion = llm_call(prompt)
        except BaseException as exc:
            row.update(outcome="RAISED", exception_type=type(exc).__name__,
                       elapsed_seconds=time.monotonic() - call_started)
            checkpoint()
            raise
        row.update(outcome="RETURNED", completion=_retained_text(completion),
                   elapsed_seconds=time.monotonic() - call_started)
        checkpoint()
        return completion

    # Preserve declared call identity without changing how the existing parser
    # or provider wrapper behaves. The trace itself never probes a judge cache.
    observed_call.cpa_cache_identity = getattr(llm_call, "cpa_cache_identity", None)
    try:
        options = {"cpa_llm_call": observed_call, "required": required}
        if context_text:
            options["context_text"] = context_text
        output = run(srt, **options)
    except BaseException as exc:
        trace.update(outcome="RAISED", exception_type=type(exc).__name__,
                     elapsed_seconds=time.monotonic() - started)
        checkpoint()
        raise
    trace.update(outcome="RETURNED", output_srt=_retained_text(output),
                 elapsed_seconds=time.monotonic() - started)
    checkpoint()
    return output
