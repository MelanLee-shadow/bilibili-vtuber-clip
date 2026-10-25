"""Small, dependency-injectable helpers for the CPA runtime boundary."""

from __future__ import annotations

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, CPA_HEALTH_MODEL, cpa_call_timeout_seconds

import json
import os
import stat
import time
import urllib.request
import socket
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

USAGE_ENV = "AUTOSLICE_CPA_USAGE_FILE"
ATTEMPT_ENV = "AUTOSLICE_PRODUCTION_ATTEMPT_ID"
CPA_RESPONSE_LOCAL_FAILURE = "CPA_RESPONSE_LOCAL_FAILURE"
_observation = ContextVar('autoslice_transport_observation',default=None)


def save_cpa_completion(response: Path, completion: Path, model: str) -> str:
    """Persist the answer before observation; distinguish invalid output from I/O failure."""
    payload = json.loads(response.read_text(encoding="utf-8"))
    text = payload.get("output_text")
    if not text:
        text = "".join(
            part["text"] for item in payload.get("output", []) or []
            if item.get("type") == "message"
            for part in item.get("content", []) or []
            if part.get("type") in ("output_text", "text") and part.get("text")
        )
    if (not isinstance(text, str) or not text.strip()
            or payload.get("status") not in (None, "completed")):
        raise ValueError("empty or incomplete CPA response")
    answering_model = payload.get("model") or model
    completion.write_text(text, encoding="utf-8")
    Path(str(completion) + ".provenance.json").write_text(
        json.dumps({"answering_model": answering_model}), encoding="utf-8")
    return answering_model


def observation_environment() -> dict[str,str]:
    context=_observation.get()
    if context is not None:
        return dict(context)
    return {key:os.environ[key] for key in (USAGE_ENV,ATTEMPT_ENV) if key in os.environ}


@contextmanager
def bind_observation(trace: Path, attempt_id: str):
    token=_observation.set({USAGE_ENV:str(trace),ATTEMPT_ENV:attempt_id})
    try:yield
    finally:_observation.reset(token)


def record_cpa_attempt(*, model: str, kind: str, status: object, elapsed_seconds: float) -> None:
    """Opt-in HTTP attempt counts, including retries; never prompts or keys."""
    row = {"model": model, "kind": kind, "status": status,
           "elapsed_seconds": round(elapsed_seconds, 3)}
    record_cpa_event('http_attempt', row)


def record_cpa_event(event_type: str, values: Mapping[str, object]) -> None:
    """Append observed transport/cache events; never infer missing requests."""
    observation=observation_environment()
    raw = observation.get(USAGE_ENV)
    if not raw:
        return
    path = Path(raw)
    row = {**values, 'event_type':event_type,
           'production_attempt_id':observation.get(ATTEMPT_ENV)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("CPA usage output must be a regular file")
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


@contextmanager
def observed_cpa_request(request, *, timeout: float, model: str, kind: str):
    started = time.monotonic()
    status = "TRANSPORT_ERROR"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            # urllib's timeout is per socket operation. Interrupt trickling
            # bodies too, so this is a total attempt bound like curl --max-time.
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            def expire():
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            timer = threading.Timer(max(0, timeout - (time.monotonic() - started)), expire)
            timer.daemon = True
            timer.start()
            try:
                yield response
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('CPA attempt deadline exceeded')
            finally:
                timer.cancel()
    except Exception as exc:
        status = getattr(exc, "code", status)
        raise
    finally:
        record_cpa_attempt(model=model, kind=kind, status=status,
                           elapsed_seconds=time.monotonic() - started)


def cpa_attempt_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not all(isinstance(row, dict) and 'kind' in row for row in rows):
            return None
        return sum(row.get('event_type', 'http_attempt') == 'http_attempt' for row in rows)
    except (OSError, ValueError):
        return None


@contextmanager
def observed_model_request(*, model: str, kind: str, transport: str):
    started, status = time.monotonic(), 'COMPLETED'
    try:
        yield
    except BaseException:
        status = 'ERROR'
        raise
    finally:
        record_cpa_event('model_request', {'model':model, 'kind':kind, 'transport':transport,
            'status':status, 'elapsed_seconds':round(time.monotonic()-started,3)})


def resolve_cpa_env_path(
    default_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve and validate the explicitly bound CPA credential file.

    Free's default remains ``<AUTOSLICE_BASE>/cpa.env``.  An isolated runner
    may bind another credential file with ``AUTOSLICE_CPA_ENV``; the override
    is validated on every use so a late environment change cannot silently
    fall back to the default.
    """

    env = os.environ if environment is None else environment
    raw_override = env.get("AUTOSLICE_CPA_ENV")
    if raw_override is None:
        return default_path
    if not raw_override:
        raise RuntimeError("AUTOSLICE_CPA_ENV must be a non-empty absolute path")
    path = Path(raw_override)
    if not path.is_absolute():
        raise RuntimeError("AUTOSLICE_CPA_ENV must be an absolute path")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"AUTOSLICE_CPA_ENV is unavailable: {path}") from exc
    if os.path.islink(path) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"AUTOSLICE_CPA_ENV must be a regular non-symlink file: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"AUTOSLICE_CPA_ENV is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError(f"AUTOSLICE_CPA_ENV must have mode 0600: {path}")
    return path


def build_cpa_qa_command(
    env_path: Path,
    *,
    load_env_file: Callable[[Path], Mapping[str, str]],
    environment: Mapping[str, str],
) -> str:
    """Build the direct semantic-QA command from the bound CPA endpoint."""

    env_file = load_env_file(env_path)
    base = (env_file.get("CPA_BASE_URL") or environment.get("CPA_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError(f"CPA_BASE_URL missing from environment and {env_path}")
    return (
        "python3 scripts/cpa_semantic_qa_llm.py --request {request_json} --response {response_json} "
        f"--transport direct --model {TOP_CPA_MODEL} --api-mode responses "
        f"--reasoning-effort medium --max-tokens 16000 --retries 3 --timeout-seconds {cpa_call_timeout_seconds()} --api-base {base} --api-key-env CPA_API_KEY"
    )


def probe_cpa_health(
    env_path: Path,
    *,
    load_env_file: Callable[[Path], Mapping[str, str]],
    environment: Mapping[str, str],
    request_factory: Callable[..., Any] = urllib.request.Request,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """Probe the same CPA failover chain used by the production batch gate."""

    env = load_env_file(env_path)
    base = (env.get("CPA_BASE_URL") or environment.get("CPA_BASE_URL", "")).rstrip("/")
    key = env.get("CPA_API_KEY") or environment.get("CPA_API_KEY", "")
    if not base or not key:
        return False
    for model in (CPA_HEALTH_MODEL,):
        body = json.dumps(
            {
                "model": model,
                "input": "回复:OK",
                "reasoning": {"effort": "low"},
                "max_output_tokens": 2000,
            }
        ).encode()
        req = request_factory(
            f"{base}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            },
        )
        try:
            with urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — any failure means this model is down
            continue
    return False
