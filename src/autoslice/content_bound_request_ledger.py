"""Durable, content-bound state for paid or otherwise side-effecting requests.

The ledger prevents a restart or ambiguous HTTP response from silently
submitting the same media job twice. It supports both one-shot calls, whose
ambiguous dispatch remains blocked for manual reconciliation, and asynchronous
submit/query calls, whose stable request UUID may resume query-only.

Only hashes, a provider request UUID, typed status codes, and sanitized audit
metadata are persisted; raw media URLs and credentials are rejected. The
record SHA-256 detects accidental corruption and ordinary stale rewrites, but
is not an authenticity proof against a malicious process running as the same
OS user.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping
import uuid


SCHEMA_VERSION = "content-bound-provider-request-ledger.v1"
MAX_LEDGER_BYTES = 1_000_000

TERMINAL_STATES = frozenset({"COMPLETED", "NO_SPEECH", "FAILED", "FAILED_PRE_DISPATCH"})
_ALLOWED_TRANSITIONS = {
    "PREPARED": frozenset({"SUBMIT_DISPATCHED", "FAILED_PRE_DISPATCH"}),
    "SUBMIT_DISPATCHED": frozenset(
        {
            "SUBMITTED",
            "SUBMIT_AMBIGUOUS",
            "PROCESSING",
            "QUEUED",
            "RESULT_AVAILABLE",
            "NO_SPEECH",
            "FAILED",
            "FAILED_PRE_DISPATCH",
        }
    ),
    "SUBMITTED": frozenset(
        {"PROCESSING", "QUEUED", "RESULT_AVAILABLE", "NO_SPEECH", "FAILED"}
    ),
    "SUBMIT_AMBIGUOUS": frozenset(
        {"PROCESSING", "QUEUED", "RESULT_AVAILABLE", "NO_SPEECH", "FAILED"}
    ),
    "PROCESSING": frozenset(
        {"PROCESSING", "QUEUED", "RESULT_AVAILABLE", "NO_SPEECH", "FAILED"}
    ),
    "QUEUED": frozenset(
        {"QUEUED", "PROCESSING", "RESULT_AVAILABLE", "NO_SPEECH", "FAILED"}
    ),
    "RESULT_AVAILABLE": frozenset({"COMPLETED", "FAILED"}),
    "COMPLETED": frozenset(),
    "NO_SPEECH": frozenset(),
    "FAILED": frozenset(),
    "FAILED_PRE_DISPATCH": frozenset(),
}

_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_key",
        "secret_key",
        "token",
        "authorization",
        "audio_url",
        "raw_url",
        "signed_url",
        "presigned_url",
        "url",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RequestLedgerError(RuntimeError):
    """Typed failure while validating or advancing a provider request ledger."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _error(reason_code: str, message: str) -> RequestLedgerError:
    return RequestLedgerError(reason_code, message)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _error("REQUEST_LEDGER_VALUE_INVALID", "ledger value is not canonical JSON") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_persisted_value(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise _error("REQUEST_LEDGER_VALUE_INVALID", "ledger object keys must be strings")
            lowered = key.casefold()
            if lowered in _FORBIDDEN_FIELD_NAMES or (
                lowered.endswith(("_api_key", "_access_key", "_secret", "_token"))
                and not lowered.endswith("_sha256")
            ):
                raise _error(
                    "REQUEST_LEDGER_SECRET_FIELD",
                    f"ledger field is not safe to persist: {'.'.join(path + (key,))}",
                )
            _validate_persisted_value(child, path=path + (key,))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_persisted_value(child, path=path + (str(index),))
        return
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered.startswith(("http://", "https://")) or lowered.startswith(
            ("bearer ", "basic ")
        ):
            raise _error("REQUEST_LEDGER_SECRET_VALUE", "raw URL or authorization value rejected")
        return
    if value is None or isinstance(value, (bool, int, float)):
        _canonical(value)
        return
    raise _error("REQUEST_LEDGER_VALUE_INVALID", "ledger contains an unsupported value")


def _validate_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(binding, Mapping) or not binding:
        raise _error("REQUEST_LEDGER_BINDING_INVALID", "request binding must be a non-empty object")
    value = dict(binding)
    _validate_persisted_value(value)
    audio_sha = value.get("input_audio_sha256")
    if not isinstance(audio_sha, str) or not _SHA256.fullmatch(audio_sha):
        raise _error(
            "REQUEST_LEDGER_BINDING_INVALID",
            "request binding must contain input_audio_sha256",
        )
    return value


def _validate_request_id(value: object) -> str:
    if not isinstance(value, str):
        raise _error("REQUEST_LEDGER_REQUEST_ID_INVALID", "provider request id is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise _error("REQUEST_LEDGER_REQUEST_ID_INVALID", "provider request id is invalid") from None
    canonical = str(parsed)
    if value.casefold() != canonical:
        raise _error("REQUEST_LEDGER_REQUEST_ID_INVALID", "provider request id is not canonical")
    return canonical


def _ensure_directory(root: Path) -> None:
    if root.is_symlink():
        raise _error("REQUEST_LEDGER_PATH_UNSAFE", "ledger root may not be a symlink")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        info = root.lstat()
    except OSError:
        raise _error("REQUEST_LEDGER_PATH_UNSAFE", "ledger root cannot be inspected") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise _error("REQUEST_LEDGER_PATH_UNSAFE", "ledger root must be owner-only")


def _read(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise _error("REQUEST_LEDGER_READ_FAILED", "request ledger cannot be opened") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 0 < info.st_size <= MAX_LEDGER_BYTES
        ):
            raise _error("REQUEST_LEDGER_PATH_UNSAFE", "request ledger file is unsafe")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_LEDGER_BYTES + 1)
        if len(raw) != info.st_size or len(raw) > MAX_LEDGER_BYTES:
            raise _error("REQUEST_LEDGER_READ_FAILED", "request ledger changed while reading")
        value = json.loads(raw.decode("utf-8"))
    except RequestLedgerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise _error("REQUEST_LEDGER_READ_FAILED", "request ledger is invalid") from None
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise _error("REQUEST_LEDGER_READ_FAILED", "request ledger is not an object")
    return value


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_LEDGER_BYTES:
        raise _error("REQUEST_LEDGER_WRITE_FAILED", "request ledger is too large")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".provider-request-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise _error("REQUEST_LEDGER_WRITE_FAILED", "request ledger cannot be persisted") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ContentBoundRequestLedger:
    """One durable state machine keyed by canonical provider request inputs."""

    def __init__(self, root: Path, binding: Mapping[str, Any]):
        self.root = Path(root).expanduser().absolute()
        self.binding = _validate_binding(binding)
        self.binding_sha256 = _digest(self.binding)
        self.path = self.root / f"{self.binding_sha256}.json"
        self.lock_path = self.root / f"{self.binding_sha256}.lock"

    @contextmanager
    def _locked(self):
        _ensure_directory(self.root)
        try:
            fd = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError:
            raise _error("REQUEST_LEDGER_LOCK_FAILED", "request ledger lock cannot be opened") from None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size != 0
            ):
                raise _error("REQUEST_LEDGER_PATH_UNSAFE", "request ledger lock is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _validate_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(record)
        supplied_hash = value.pop("record_sha256", None)
        if value.get("schema_version") != SCHEMA_VERSION:
            raise _error("REQUEST_LEDGER_SCHEMA_MISMATCH", "request ledger schema differs")
        if value.get("binding") != self.binding or value.get("binding_sha256") != self.binding_sha256:
            raise _error("REQUEST_LEDGER_BINDING_MISMATCH", "request ledger binding differs")
        if supplied_hash != _digest(value):
            raise _error("REQUEST_LEDGER_HASH_MISMATCH", "request ledger self-hash differs")
        _validate_request_id(value.get("request_id"))
        expected_record_fields = {
            "schema_version",
            "binding",
            "binding_sha256",
            "request_id",
            "state",
            "revision",
            "events",
        }
        if set(value) != expected_record_fields:
            raise _error("REQUEST_LEDGER_STATE_INVALID", "request ledger fields are invalid")
        state = value.get("state")
        revision = value.get("revision")
        events = value.get("events")
        if (
            state not in _ALLOWED_TRANSITIONS
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(events, list)
            or len(events) != revision
        ):
            raise _error("REQUEST_LEDGER_STATE_INVALID", "request ledger state is invalid")
        prior_state: str | None = None
        expected_event_fields = {
            "sequence",
            "state",
            "event",
            "reason_code",
            "metadata",
        }
        for index, event in enumerate(events, 1):
            if (
                not isinstance(event, dict)
                or set(event) != expected_event_fields
                or event.get("sequence") != index
                or event.get("state") not in _ALLOWED_TRANSITIONS
                or not isinstance(event.get("event"), str)
                or not event["event"].strip()
                or (
                    event.get("reason_code") is not None
                    and (
                        not isinstance(event.get("reason_code"), str)
                        or not event["reason_code"]
                    )
                )
                or not isinstance(event.get("metadata"), dict)
            ):
                raise _error("REQUEST_LEDGER_STATE_INVALID", "request ledger event is invalid")
            _validate_persisted_value(event)
            event_state = event["state"]
            if index == 1:
                if (
                    event_state != "PREPARED"
                    or event["event"] != "REQUEST_ID_PREPARED"
                    or event["reason_code"] is not None
                    or event["metadata"] != {}
                ):
                    raise _error(
                        "REQUEST_LEDGER_STATE_INVALID",
                        "request ledger initial event is invalid",
                    )
            elif prior_state is None or event_state not in _ALLOWED_TRANSITIONS[prior_state]:
                raise _error(
                    "REQUEST_LEDGER_TRANSITION_INVALID",
                    f"request ledger history cannot transition from {prior_state} to {event_state}",
                )
            prior_state = event_state
        if prior_state != state:
            raise _error("REQUEST_LEDGER_STATE_INVALID", "request ledger tail state differs")
        return {**value, "record_sha256": supplied_hash}

    def _seal(self, value: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = dict(value)
        unsigned.pop("record_sha256", None)
        _validate_persisted_value(unsigned)
        return {**unsigned, "record_sha256": _digest(unsigned)}

    def prepare(self, *, request_id: str | None = None) -> dict[str, Any]:
        """Create the one request UUID, or return the previously bound state."""

        with self._locked():
            if os.path.lexists(self.path):
                return self._validate_record(_read(self.path))
            canonical_id = _validate_request_id(request_id or str(uuid.uuid4()))
            record = self._seal(
                {
                    "schema_version": SCHEMA_VERSION,
                    "binding": self.binding,
                    "binding_sha256": self.binding_sha256,
                    "request_id": canonical_id,
                    "state": "PREPARED",
                    "revision": 1,
                    "events": [
                        {
                            "sequence": 1,
                            "state": "PREPARED",
                            "event": "REQUEST_ID_PREPARED",
                            "reason_code": None,
                            "metadata": {},
                        }
                    ],
                }
            )
            _write(self.path, record)
            return record

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            if not os.path.lexists(self.path):
                raise _error("REQUEST_LEDGER_MISSING", "request ledger has not been prepared")
            return self._validate_record(_read(self.path))

    def transition(
        self,
        new_state: str,
        *,
        event: str,
        reason_code: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Advance the persisted state exactly once under the content lock."""

        if new_state not in _ALLOWED_TRANSITIONS:
            raise _error("REQUEST_LEDGER_STATE_INVALID", "requested ledger state is invalid")
        if not isinstance(event, str) or not event.strip():
            raise _error("REQUEST_LEDGER_EVENT_INVALID", "ledger event name is invalid")
        if reason_code is not None and (not isinstance(reason_code, str) or not reason_code):
            raise _error("REQUEST_LEDGER_EVENT_INVALID", "ledger reason code is invalid")
        event_metadata = dict(metadata or {})
        _validate_persisted_value(event_metadata)
        with self._locked():
            if not os.path.lexists(self.path):
                raise _error("REQUEST_LEDGER_MISSING", "request ledger has not been prepared")
            record = self._validate_record(_read(self.path))
            current = record["state"]
            if new_state not in _ALLOWED_TRANSITIONS[current]:
                raise _error(
                    "REQUEST_LEDGER_TRANSITION_INVALID",
                    f"request ledger cannot transition from {current} to {new_state}",
                )
            revision = record["revision"] + 1
            events = list(record["events"])
            events.append(
                {
                    "sequence": revision,
                    "state": new_state,
                    "event": event.strip(),
                    "reason_code": reason_code,
                    "metadata": event_metadata,
                }
            )
            updated = self._seal(
                {
                    **record,
                    "state": new_state,
                    "revision": revision,
                    "events": events,
                }
            )
            _write(self.path, updated)
            return updated


__all__ = [
    "ContentBoundRequestLedger",
    "MAX_LEDGER_BYTES",
    "RequestLedgerError",
    "SCHEMA_VERSION",
    "TERMINAL_STATES",
]
