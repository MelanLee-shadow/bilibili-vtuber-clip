"""Durable retry pacing for the serialized publication queue.

The upload ledger is the primary crash-safe source for failed remote attempts.
A small self-hashed sidecar supplements it for failures that occur before an
upload attempt is appended, notably the account-wide rolling-quota guard.
Neither source authorizes publication or weakens the existing upload gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "publication-queue-backoff.v1"
STATE_FILENAME = "publication_queue_backoff.json"
GENERIC_BASE_SECONDS = 15 * 60
GENERIC_CAP_SECONDS = 6 * 60 * 60
QUOTA_SECONDS = 24 * 60 * 60
QUOTA_FREQUENCY_CODE = 21566
_MAX_JSON_BYTES = 1_000_000


class PublicationQueueBackoffError(ValueError):
    """A backoff source is malformed, unsafe, or inconsistent."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_runtime_root(value: str | Path) -> Path:
    raw = Path(value).expanduser().absolute()
    try:
        info = os.lstat(raw)
    except OSError as exc:
        raise PublicationQueueBackoffError("runtime root is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PublicationQueueBackoffError("runtime root is unsafe")
    return raw


def _state_path(runtime_root: Path) -> Path:
    return runtime_root / "reports" / STATE_FILENAME


def _parse_epoch(value: object) -> float:
    if not isinstance(value, str) or not value:
        raise PublicationQueueBackoffError("ledger timestamp is missing")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PublicationQueueBackoffError("ledger timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PublicationQueueBackoffError("ledger timestamp has no timezone")
    return parsed.timestamp()


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _safe_manifest(path_value: object, *, runtime_root: Path) -> tuple[Path, dict[str, Any], str]:
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        raise PublicationQueueBackoffError("manifest path is missing")
    raw = Path(path_value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise PublicationQueueBackoffError("manifest path is unsafe")
    try:
        resolved = raw.resolve(strict=True)
        resolved.relative_to(runtime_root)
        info = os.lstat(raw)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PublicationQueueBackoffError("manifest is unavailable or outside runtime") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PublicationQueueBackoffError("manifest must be a single-link regular file")
    if not 0 < info.st_size <= _MAX_JSON_BYTES:
        raise PublicationQueueBackoffError("manifest size is invalid")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationQueueBackoffError("manifest JSON is invalid") from exc
    if not isinstance(document, dict):
        raise PublicationQueueBackoffError("manifest root is not an object")
    return resolved, document, _sha256(resolved)


def _manifest_identity(path_value: object, *, runtime_root: Path) -> dict[str, str]:
    path, document, manifest_sha = _safe_manifest(path_value, runtime_root=runtime_root)
    video = document.get("video")
    if not isinstance(video, Mapping):
        raise PublicationQueueBackoffError("manifest video entry is invalid")
    video_sha = str(video.get("sha256") or "").removeprefix("sha256:")
    if len(video_sha) != 64 or any(char not in "0123456789abcdef" for char in video_sha.lower()):
        raise PublicationQueueBackoffError("manifest video SHA256 is invalid")
    return {
        "manifest_path": str(path),
        "manifest_sha256": manifest_sha,
        "video_sha256": video_sha.lower(),
    }


def _row_sha256(row: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(dict(row)) + b"\n").hexdigest()


def _matching_failed_rows(
    entries: Sequence[Mapping[str, object]],
    *,
    identity: Mapping[str, str],
) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for row in entries:
        if row.get("event") != "UPLOAD_ATTEMPT_FINISHED":
            continue
        try:
            row_manifest = str(Path(str(row.get("manifest") or "")).resolve())
        except (OSError, RuntimeError):
            continue
        row_manifest_sha = str(row.get("manifest_sha256") or "").removeprefix("sha256:")
        row_video_sha = str(row.get("video_sha256") or "").removeprefix("sha256:")
        if (
            row_manifest != identity["manifest_path"]
            or row_manifest_sha != identity["manifest_sha256"]
            or row_video_sha != identity["video_sha256"]
        ):
            continue
        rows.append(row)
    return rows


def _ledger_backoff(
    entries: Sequence[Mapping[str, object]],
    *,
    identity: Mapping[str, str],
    now_epoch: float,
) -> dict[str, object] | None:
    active: list[dict[str, object]] = []
    for row in entries:
        if (
            row.get("event") == "UPLOAD_ATTEMPT_FINISHED"
            and row.get("quota_frequency_code") == QUOTA_FREQUENCY_CODE
        ):
            failed_at = _parse_epoch(row.get("at"))
            next_epoch = failed_at + QUOTA_SECONDS
            if next_epoch > now_epoch:
                active.append(
                    {
                        "scope": "GLOBAL_QUOTA",
                        "reason_code": "PUBLICATION_QUEUE_QUOTA_BACKOFF",
                        "failure_count": 1,
                        "last_rc": row.get("rc"),
                        "last_failure_at": _iso_utc(failed_at),
                        "next_attempt_at": _iso_utc(next_epoch),
                        "next_attempt_epoch": next_epoch,
                        "source": "UPLOAD_LEDGER",
                        "ledger_row_sha256": _row_sha256(row),
                    }
                )
    matching = _matching_failed_rows(entries, identity=identity)
    consecutive: list[Mapping[str, object]] = []
    for row in reversed(matching):
        rc = row.get("rc")
        uploader_rc = row.get("uploader_rc")
        if rc == 0 or (uploader_rc == 0 and row.get("bvid")):
            break
        if (
            isinstance(rc, bool)
            or not isinstance(rc, int)
            or rc == 0
            or isinstance(uploader_rc, bool)
            or not isinstance(uploader_rc, int)
            or uploader_rc == 0
            or row.get("bvid")
        ):
            break
        consecutive.append(row)
    if consecutive:
        latest = consecutive[0]
        failed_at = _parse_epoch(latest.get("at"))
        delay = min(
            GENERIC_BASE_SECONDS * (2 ** (len(consecutive) - 1)),
            GENERIC_CAP_SECONDS,
        )
        next_epoch = failed_at + delay
        if next_epoch > now_epoch:
            active.append(
                {
                    "scope": "MANIFEST",
                    "reason_code": "PUBLICATION_QUEUE_RETRY_BACKOFF",
                    "failure_count": len(consecutive),
                    "last_rc": latest.get("rc"),
                    "last_failure_at": _iso_utc(failed_at),
                    "next_attempt_at": _iso_utc(next_epoch),
                    "next_attempt_epoch": next_epoch,
                    "source": "UPLOAD_LEDGER",
                    "ledger_row_sha256": _row_sha256(latest),
                    **identity,
                }
            )
    if not active:
        return None
    return max(active, key=lambda row: float(row["next_attempt_epoch"]))


def _load_sidecar(runtime_root: Path) -> dict[str, object] | None:
    path = _state_path(runtime_root)
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise PublicationQueueBackoffError("backoff sidecar is a symlink")
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PublicationQueueBackoffError("backoff sidecar is not single-link regular")
    if not 0 < info.st_size <= _MAX_JSON_BYTES:
        raise PublicationQueueBackoffError("backoff sidecar size is invalid")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationQueueBackoffError("backoff sidecar JSON is invalid") from exc
    if not isinstance(document, dict):
        raise PublicationQueueBackoffError("backoff sidecar root is invalid")
    body = dict(document)
    declared = body.pop("state_sha256", None)
    required = {
        "schema_version",
        "scope",
        "reason_code",
        "failure_count",
        "last_rc",
        "last_status",
        "last_failure_at",
        "next_attempt_at",
        "next_attempt_epoch",
        "source",
        "manifest_path",
        "manifest_sha256",
        "video_sha256",
        "updated_at",
    }
    if set(body) != required or body.get("schema_version") != SCHEMA_VERSION:
        raise PublicationQueueBackoffError("backoff sidecar fields are invalid")
    if declared != _canonical_sha256(body):
        raise PublicationQueueBackoffError("backoff sidecar self-hash is invalid")
    if body.get("scope") not in {"GLOBAL_QUOTA", "MANIFEST"}:
        raise PublicationQueueBackoffError("backoff sidecar scope is invalid")
    if (
        isinstance(body.get("failure_count"), bool)
        or not isinstance(body.get("failure_count"), int)
        or int(body["failure_count"]) < 1
        or isinstance(body.get("next_attempt_epoch"), bool)
        or not isinstance(body.get("next_attempt_epoch"), (int, float))
    ):
        raise PublicationQueueBackoffError("backoff sidecar counters are invalid")
    body["state_sha256"] = declared
    body["path"] = str(path)
    return body


def _sidecar_backoff(
    state: Mapping[str, object] | None,
    *,
    identity: Mapping[str, str],
    now_epoch: float,
) -> dict[str, object] | None:
    if state is None or float(state["next_attempt_epoch"]) <= now_epoch:
        return None
    if state.get("scope") == "MANIFEST" and any(
        state.get(key) != identity[key]
        for key in ("manifest_path", "manifest_sha256", "video_sha256")
    ):
        return None
    return {
        key: state[key]
        for key in (
            "scope",
            "reason_code",
            "failure_count",
            "last_rc",
            "last_status",
            "last_failure_at",
            "next_attempt_at",
            "next_attempt_epoch",
            "source",
            "manifest_path",
            "manifest_sha256",
            "video_sha256",
        )
    } | {"state_path": state["path"], "state_sha256": state["state_sha256"]}


def evaluate_upload_backoff(
    *,
    runtime_root: str | Path,
    manifest_path: str | Path,
    ledger_entries: Sequence[Mapping[str, object]],
    now_epoch: float | None = None,
) -> dict[str, object]:
    now_value = time.time() if now_epoch is None else float(now_epoch)
    try:
        root = _absolute_runtime_root(runtime_root)
        identity = _manifest_identity(manifest_path, runtime_root=root)
        sidecar = _sidecar_backoff(
            _load_sidecar(root), identity=identity, now_epoch=now_value
        )
        ledger = _ledger_backoff(
            ledger_entries, identity=identity, now_epoch=now_value
        )
    except (OSError, RuntimeError, PublicationQueueBackoffError) as exc:
        return {
            "status": "INVALID",
            "reason_code": "PUBLICATION_QUEUE_BACKOFF_STATE_INVALID",
            "detail": str(exc)[:512],
        }
    candidates = [row for row in (sidecar, ledger) if row is not None]
    if not candidates:
        return {"status": "CLEAR", **identity}
    selected = max(candidates, key=lambda row: float(row["next_attempt_epoch"]))
    return {
        "status": "BACKOFF",
        **selected,
        "retry_after_seconds": max(
            0, int(float(selected["next_attempt_epoch"]) - now_value)
        ),
    }


def _write_atomic(path: Path, document: Mapping[str, object]) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise PublicationQueueBackoffError("backoff sidecar parent is unsafe")
    if os.path.lexists(path) and path.is_symlink():
        raise PublicationQueueBackoffError("backoff sidecar is a symlink")
    body = (
        json.dumps(
            dict(document),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary = parent / f".{path.name}.tmp-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while creating backoff sidecar")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def record_queue_result(
    *,
    runtime_root: str | Path,
    manifest_path: str | Path,
    rc: int,
    status: str,
    ledger_entries: Sequence[Mapping[str, object]],
    now_epoch: float | None = None,
) -> dict[str, object]:
    now_value = time.time() if now_epoch is None else float(now_epoch)
    try:
        root = _absolute_runtime_root(runtime_root)
        identity = _manifest_identity(manifest_path, runtime_root=root)
        path = _state_path(root)
        if rc in {0, 3, 6}:
            state = _load_sidecar(root)
            if state is None:
                return {"status": "CLEARED"}
            scope = state.get("scope")
            same_manifest = all(
                state.get(key) == identity[key]
                for key in ("manifest_path", "manifest_sha256", "video_sha256")
            )
            should_clear = (
                (scope == "MANIFEST" and same_manifest)
                or (scope == "GLOBAL_QUOTA" and rc in {0, 6})
            )
            if not should_clear:
                return {
                    "status": "PRESERVED",
                    "path": state["path"],
                    "scope": scope,
                    "reason_code": state["reason_code"],
                    "manifest_path": state["manifest_path"],
                    "state_sha256": state["state_sha256"],
                }
            path.unlink()
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {"status": "CLEARED"}
        if rc == 8:
            scope = "GLOBAL_QUOTA"
            reason = "PUBLICATION_QUEUE_QUOTA_BACKOFF"
            failures = 1
            next_epoch = now_value + QUOTA_SECONDS
            source = "QUEUE_RESULT"
        elif rc not in {2, 5}:
            ledger = _ledger_backoff(
                ledger_entries, identity=identity, now_epoch=now_value
            )
            scope = "MANIFEST"
            reason = "PUBLICATION_QUEUE_RETRY_BACKOFF"
            failures = int((ledger or {}).get("failure_count") or 1)
            next_epoch = float(
                (ledger or {}).get("next_attempt_epoch")
                or now_value
                + min(
                    GENERIC_BASE_SECONDS * (2 ** (failures - 1)),
                    GENERIC_CAP_SECONDS,
                )
            )
            source = "QUEUE_RESULT+UPLOAD_LEDGER" if ledger else "QUEUE_RESULT"
        else:
            return {"status": "UNCHANGED"}
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "scope": scope,
            "reason_code": reason,
            "failure_count": failures,
            "last_rc": rc,
            "last_status": status,
            "last_failure_at": _iso_utc(now_value),
            "next_attempt_at": _iso_utc(next_epoch),
            "next_attempt_epoch": next_epoch,
            "source": source,
            "manifest_path": identity["manifest_path"],
            "manifest_sha256": identity["manifest_sha256"],
            "video_sha256": identity["video_sha256"],
            "updated_at": _iso_utc(now_value),
        }
        document["state_sha256"] = _canonical_sha256(document)
        _write_atomic(path, document)
        return {"status": "RECORDED", "path": str(path), **document}
    except (OSError, RuntimeError, PublicationQueueBackoffError) as exc:
        return {
            "status": "INVALID",
            "reason_code": "PUBLICATION_QUEUE_BACKOFF_STATE_INVALID",
            "detail": str(exc)[:512],
        }


__all__ = [
    "GENERIC_BASE_SECONDS",
    "GENERIC_CAP_SECONDS",
    "QUOTA_SECONDS",
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "evaluate_upload_backoff",
    "record_queue_result",
]
