"""Small, dependency-injectable helpers for the CPA runtime boundary."""

from __future__ import annotations


import json
import os
import stat
import time
import urllib.request
import socket
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from pathlib import Path

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
        from src.autoslice.provider_slots import report_provider_feedback
        report_provider_feedback(exc)
        raise
    finally:
        record_cpa_attempt(model=model, kind=kind, status=status,
                           elapsed_seconds=time.monotonic() - started)


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


