"""Explicit, immutable extra TEXT attempts for one exhausted durable request.

Only an operator acting on verified human authorization issues a grant. A grant
retains the original state and binds its existing event prefix and wire input;
it does not authorize audio, another request, or replay of a completed response.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import NoReturn, cast

from .infra_retry_policy import CPA_TEXT_MAX_ATTEMPTS

GRANT_FILE = "retry-grant.json"
SCHEMA = "h104-text-retry-grant-v1"


def _refuse() -> NoReturn:
    from .semantic_retry_progress import RetryNeedsPlan
    raise RetryNeedsPlan("CPA_TEXT_RETRY_GRANT_INVALID")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _sha(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _authority(value: object) -> dict[str, str]:
    keys = {"source", "thread_id", "message_id", "recorded_at", "message_text", "message_sha256"}
    if (not isinstance(value, dict) or set(value) != keys
            or not all(isinstance(v, str) and v.strip() for v in value.values())):
        _refuse()
    result = cast(dict[str, str], value)
    if (result["source"] != "human"
            or hashlib.sha256(result["message_text"].encode()).hexdigest() != result["message_sha256"]):
        _refuse()
    return dict(result)


@dataclass(frozen=True)
class RetryGrant:
    max_attempts: int
    input_identity: str
    prompt_sha256: str
    sha256: str

    def accepts(self, input_identity: str, prompt_sha256: str) -> bool:
        return self.input_identity == input_identity and self.prompt_sha256 == prompt_sha256


def _validate(document: object, logical_request: str, state: Mapping[str, object]) -> RetryGrant:
    if not isinstance(document, dict):
        _refuse()
    expected = {"schema", "logical_request", "input_identity", "prompt_sha256", "base_max_attempts",
                "base_event_count", "base_events_sha256", "additional_attempts", "authority"}
    if set(document) != expected:
        _refuse()
    grant = cast(dict[str, object], document)
    count, additional = grant["base_event_count"], grant["additional_attempts"]
    events = state.get("events")
    if (grant["schema"] != SCHEMA or grant["logical_request"] != logical_request
            or grant["base_max_attempts"] != CPA_TEXT_MAX_ATTEMPTS
            or type(grant["base_max_attempts"]) is not int
            or type(count) is not int or count <= 0
            or type(additional) is not int or additional <= 0
            or not isinstance(events, list) or count > len(events)
            or not all(_sha(grant[key]) for key in ("input_identity", "prompt_sha256", "base_events_sha256"))):
        _refuse()
    prefix = events[:count]
    if (_digest(prefix) != grant["base_events_sha256"]
            or not all(isinstance(event, dict) for event in prefix)):
        _refuse()
    starts = [event for event in prefix if event.get("status") == "STARTED"]
    if ([event.get("attempt") for event in starts] != list(range(1, CPA_TEXT_MAX_ATTEMPTS + 1))
            or any(event.get("input_identity") != grant["input_identity"] for event in prefix)
            or any(event.get("status") not in ("STARTED", "OUTCOME_UNKNOWN") for event in prefix)):
        _refuse()
    for event in events[count:]:
        if (not isinstance(event, dict) or type(event.get("attempt")) is not int
                or event["attempt"] <= CPA_TEXT_MAX_ATTEMPTS
                or event.get("input_identity") != grant["input_identity"]):
            _refuse()
    _authority(grant["authority"])
    return RetryGrant(CPA_TEXT_MAX_ATTEMPTS + additional, cast(str, grant["input_identity"]),
                      cast(str, grant["prompt_sha256"]), _digest(grant))


def load_retry_grant(folder: Path, logical_request: str, state: Mapping[str, object]) -> RetryGrant | None:
    from .qixi_transaction_core import stable_regular_snapshot
    path = folder / GRANT_FILE
    if not path.exists() and not path.is_symlink():
        if state.get("retry_grant_sha256") is not None:
            _refuse()
        return None
    if folder.name != "text-" + logical_request:
        _refuse()
    try:
        snapshot = stable_regular_snapshot(path, label="text retry authorization")
        if snapshot is None:
            _refuse()
        result = _validate(json.loads(snapshot.payload), logical_request, state)
        if state.get("retry_grant_sha256") != result.sha256:
            _refuse()
        return result
    except (OSError, ValueError, TypeError):
        _refuse()


def issue_retry_grant(
    folder: Path, logical_request: str, state: Mapping[str, object], *,
    input_identity: str, prompt_sha256: str, additional_attempts: int,
    authority: Mapping[str, str],
) -> RetryGrant:
    """Called under TextRequest.claim; verifies the existing exhausted prefix.

    The authority record is a transcription, not proof of a human decision.
    Its issuer must first verify the referenced human message and its scope.
    """
    if (folder.name != "text-" + logical_request
            or state.get("reserved_attempts") != CPA_TEXT_MAX_ATTEMPTS
            or state.get("max_attempts") != CPA_TEXT_MAX_ATTEMPTS
            or not isinstance(state.get("events"), list)):
        _refuse()
    events = cast(list[object], state["events"])
    document = {"schema": SCHEMA, "logical_request": logical_request,
                "input_identity": input_identity, "prompt_sha256": prompt_sha256,
                "base_max_attempts": CPA_TEXT_MAX_ATTEMPTS,
                "base_event_count": len(events), "base_events_sha256": _digest(events),
                "additional_attempts": additional_attempts, "authority": dict(authority)}
    result = _validate(document, logical_request, state)
    try:
        fd = os.open(folder / GRANT_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _refuse()
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return result
