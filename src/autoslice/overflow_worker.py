"""Durable, single-slot control protocol for autoslice overflow jobs."""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

REQUEST_SCHEMA = "autoslice-overflow-request.v2"
LEGACY_REQUEST_SCHEMA = "autoslice-overflow-request.v1"
STATE_SCHEMA = "autoslice-overflow-state.v1"
DEFAULT_WORKER_ROOT = Path("/data/autoslice-worker/control")
LEGACY_ADOPTION_SCHEMA = "autoslice-legacy-store-adoption.v1"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_REQUIRED = {
    "schema_version", "job_id", "attempt_id", "release_sha256", "input_sha256",
    "lane", "date", "item", "runtime_root", "controller_root",
    "estimated_peak_bytes", "operational_reserve_bytes",
}
_OPTIONAL = {"source_files", "produce_kwargs", "profile_binding", "worker_store_id"}
_ACTIVE = {"submitted", "running"}
_TERMINAL = {"completed", "failed"}
_STARTUP_PRE_EXECUTOR = "pre_executor"
_STARTUP_PRODUCER_IMPORT = "producer_import"
_STARTUP_EXECUTOR_STARTED = "executor_started"


class OverflowWorkerError(RuntimeError):
    code = "worker_error"


class RequestValidationError(OverflowWorkerError):
    code = "request_invalid"


class PathValidationError(RequestValidationError):
    code = "path_invalid"


class JobConflictError(OverflowWorkerError):
    code = "job_conflict"


class DuplicateJobError(JobConflictError):
    code = "duplicate_job"


class SlotBusyError(OverflowWorkerError):
    code = "slot_busy"


class InsufficientDiskError(OverflowWorkerError):
    code = "insufficient_disk"


class JobNotFoundError(OverflowWorkerError):
    code = "job_not_found"


class JobUnknownError(OverflowWorkerError):
    code = "job_unknown"


class StateIntegrityError(OverflowWorkerError):
    code = "state_integrity_error"


class WorkerAuthorityError(OverflowWorkerError):
    code = "worker_authority_unknown"


class ResultIntegrityError(OverflowWorkerError):
    code = "result_integrity_error"


class RuntimePrerequisiteError(OverflowWorkerError):
    """The worker runtime cannot safely enter the producer adapter."""

    code = "runtime_prerequisite_failed"
    prefix = "RUNTIME_PREREQUISITE:"

    def __init__(self, message: str, *, startup_stage: str | None = None):
        text = str(message)
        if not text.startswith(self.prefix):
            text = f"{self.prefix} {text}"
        super().__init__(text)
        self.startup_stage = startup_stage


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                           sort_keys=True, separators=(",", ":")) + "\n").encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RequestValidationError("value is not canonical JSON") from exc


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def request_identity_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """The stable identity used by the controller for one candidate."""
    item = request.get("item")
    if not isinstance(item, dict):
        raise RequestValidationError("item must be a mapping")
    body = {
        "date": request.get("date"),
        "lane": request.get("lane"),
        "item": {k: v for k, v in item.items() if k != "runtime_scratch_dir"},
        "release_sha256": request.get("release_sha256"),
    }
    if "profile_binding" in request:
        from src.autoslice.channel_profile import normalize_profile_binding
        body["profile_binding"] = normalize_profile_binding(request["profile_binding"])
    return body


def compute_job_id(request: Mapping[str, Any]) -> str:
    """Compute the content identity shared with ``SSHBackend._identity``."""
    return _sha(_json_bytes(request_identity_payload(request)))


def request_sha256(request: Mapping[str, Any]) -> str:
    return _sha(_json_bytes(dict(request)))


def _abs(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or not Path(value).is_absolute():
        raise PathValidationError(f"{name} is not an absolute path")
    return value


def _resolved(value: str) -> Path:
    try:
        return Path(value).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathValidationError("path cannot be resolved") from exc


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_sources(rows: Any) -> None:
    if not isinstance(rows, list):
        raise RequestValidationError("source_files must be a list")
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (
            {"path", "bytes", "sha256"}, {"path", "size", "sha256"}
        ):
            raise RequestValidationError("source_files entry is invalid")
        _abs(row["path"], "source_files.path")
        size = row.get("bytes", row.get("size"))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RequestValidationError("source_files size is invalid")
        if not isinstance(row["sha256"], str) or not _HEX64.fullmatch(row["sha256"]):
            raise RequestValidationError("source_files hash is invalid")


def validate_request(
    request: Mapping[str, Any], *,
    allowed_runtime_parent: str | os.PathLike[str] | None = None,
    expected_runtime_root: str | os.PathLike[str] | None = None,
    allow_legacy_readonly: bool = False,
) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise RequestValidationError("request must be an object")
    keys = set(request)
    if keys & _REQUIRED != _REQUIRED or keys - _REQUIRED - _OPTIONAL:
        raise RequestValidationError("request fields are incomplete")
    legacy = request.get("schema_version") == LEGACY_REQUEST_SCHEMA
    if legacy and not allow_legacy_readonly:
        raise RequestValidationError("legacy request cannot execute without task profile binding; status/fetch only")
    if request.get("schema_version") != REQUEST_SCHEMA and not (legacy and allow_legacy_readonly):
        raise RequestValidationError("request schema is unsupported")
    if not (legacy and allow_legacy_readonly and "profile_binding" not in request):
        from src.autoslice.channel_profile import ChannelProfileError, normalize_profile_binding
        try:
            normalize_profile_binding(request.get("profile_binding"))
        except ChannelProfileError as exc:
            raise RequestValidationError(str(exc)) from exc
    if not isinstance(request.get("job_id"), str) or not _HEX64.fullmatch(request["job_id"]):
        raise RequestValidationError("job_id is invalid")
    if not isinstance(request.get("attempt_id"), str) or not _HEX32.fullmatch(request["attempt_id"]):
        raise RequestValidationError("attempt_id is invalid")
    for field in ("release_sha256", "input_sha256"):
        if not isinstance(request.get(field), str) or not _HEX64.fullmatch(request[field]):
            raise RequestValidationError(f"{field} is invalid")
    if request.get("lane") not in {"talk", "song"}:
        raise RequestValidationError("lane is invalid")
    if not isinstance(request.get("date"), str) or not _DATE.fullmatch(request["date"]):
        raise RequestValidationError("date is invalid")
    try:
        dt.date.fromisoformat(request["date"])
    except ValueError as exc:
        raise RequestValidationError("date is invalid") from exc
    if not isinstance(request.get("item"), dict):
        raise RequestValidationError("item must be a mapping")
    runtime = _abs(request.get("runtime_root"), "runtime_root")
    _abs(request.get("controller_root"), "controller_root")
    if expected_runtime_root is not None and runtime != _abs(str(expected_runtime_root), "runtime_root"):
        raise PathValidationError("runtime_root does not match configured runtime")
    if allowed_runtime_parent is not None:
        parent = _abs(str(allowed_runtime_parent), "allowed_runtime_parent")
        if not _inside(_resolved(runtime), _resolved(parent)):
            raise PathValidationError("runtime_root is outside allowed runtime parent")
    peak, reserve = request.get("estimated_peak_bytes"), request.get("operational_reserve_bytes")
    if isinstance(peak, bool) or not isinstance(peak, int) or peak <= 0:
        raise RequestValidationError("estimated_peak_bytes is invalid")
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
        raise RequestValidationError("operational_reserve_bytes is invalid")
    if "source_files" in request:
        _validate_sources(request["source_files"])
    if "produce_kwargs" in request and not isinstance(request["produce_kwargs"], dict):
        raise RequestValidationError("produce_kwargs must be a mapping")
    if "worker_store_id" in request and (not isinstance(request["worker_store_id"], str)
                                        or not _HEX32.fullmatch(request["worker_store_id"])):
        raise RequestValidationError("worker_store_id is invalid")
    normalized = json.loads(_json_bytes(dict(request)).decode())
    if request["job_id"] != compute_job_id(normalized):
        raise RequestValidationError("job_id does not match request identity")
    return normalized


def new_attempt_id() -> str:
    return uuid.uuid4().hex


def _directory(path: Path, *, create: bool = True) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise JobNotFoundError("job is absent")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(path)
    except OSError as exc:
        raise OverflowWorkerError("worker directory cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PathValidationError("worker directory is unsafe")


def _regular(path: Path, *, absent_ok: bool = False) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if absent_ok:
            return
        raise JobNotFoundError("worker file is absent")
    except OSError as exc:
        raise StateIntegrityError("worker file cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StateIntegrityError("worker file is unsafe")


def _sync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    os.close(fd)


def _write(path: Path, value: Any, *, exclusive: bool = False) -> None:
    _directory(path.parent)
    if exclusive and os.path.lexists(path):
        raise JobConflictError("worker file already exists")
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read(path: Path, label: str) -> Any:
    _regular(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateIntegrityError(f"{label} is invalid JSON") from exc


def _store_id(root: Path, *, initialize: bool = False) -> str | None:
    """Read a durable store identity; initialize only an empty, locked store.

    Callers that initialize hold the existing worker slot.  Missing metadata
    beside jobs is unknown, never permission to stamp a replacement identity.
    A copied marker cannot identify a physical volume or detect other deletion.
    """
    marker = root / "worker-store.json"
    try:
        value = _read(marker, "worker store authority")
    except JobNotFoundError:
        if not initialize:
            return None
        jobs = root / "jobs"
        _directory(jobs, create=False)
        if any(p.name != ".worker-slot.lock" for p in jobs.iterdir()):
            raise WorkerAuthorityError("store identity missing beside jobs; restore original authority")
        value = {"schema_version": "autoslice-worker-store.v1", "store_id": uuid.uuid4().hex}
        _write(marker, value, exclusive=True)
    if (not isinstance(value, dict)
            or not {"schema_version", "store_id"} <= set(value)
            or set(value) - {"schema_version", "store_id", "legacy_adoption_sha256"}
            or value["schema_version"] != "autoslice-worker-store.v1"
            or not isinstance(value["store_id"], str) or not _HEX32.fullmatch(value["store_id"])
            or ("legacy_adoption_sha256" in value and
                (not isinstance(value["legacy_adoption_sha256"], str)
                 or not _HEX64.fullmatch(value["legacy_adoption_sha256"])))):
        raise WorkerAuthorityError("store identity is invalid; restore original authority")
    return value["store_id"]


def _adoption_bytes(path: Path) -> bytes:
    # Adoption accepts named original files, never symlink aliases or .. paths.
    if not path.is_absolute() or path.resolve() != path:
        raise PathValidationError("legacy adoption path is unsafe")
    _regular(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        payload = handle.read()
        after = os.fstat(handle.fileno())
    current = path.lstat()
    def fingerprint(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if (not stat.S_ISREG(before.st_mode) or fingerprint(before) != fingerprint(after)
            or fingerprint(after) != fingerprint(current)):
        raise StateIntegrityError("legacy adoption input changed while reading")
    return payload


def _adoption_files(jobdir: Path) -> dict[str, str]:
    allowed = {"request.json", "state.json", "result.json", "worker.log", "bundle"}
    for entry in jobdir.iterdir():
        if entry.name not in allowed:
            raise StateIntegrityError("unknown legacy attempt entry")
        if entry.name == "bundle":
            _directory(entry, create=False)
        else:
            _regular(entry)
    files = {}
    for current, dirs, names in os.walk(jobdir, followlinks=False):
        directory = Path(current)
        for name in dirs:
            child = directory / name
            _directory(child, create=False)
            if child.resolve() != child:
                raise PathValidationError("legacy adoption directory is unsafe")
        for name in names:
            child = directory / name
            files[str(child.relative_to(jobdir))] = _sha(_adoption_bytes(child))
    return files


def _adoption_snapshot(root: Path, rows: list[dict[str, Any]], parent: str,
                       expected_runtime_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    found = {}
    for jobroot in (root / "jobs").iterdir():
        if jobroot.name == ".worker-slot.lock":
            _regular(jobroot)
            continue
        _directory(jobroot, create=False)
        if not _HEX64.fullmatch(jobroot.name):
            raise StateIntegrityError("unknown legacy job entry")
        attempts = list(jobroot.iterdir())
        if not attempts:
            raise StateIntegrityError("legacy job has no attempt")
        for attempt in attempts:
            _directory(attempt, create=False)
            if not _HEX32.fullmatch(attempt.name) or len(found) >= 50:
                raise StateIntegrityError("legacy attempt inventory is unsafe or exceeds 50")
            found[(jobroot.name, attempt.name)] = attempt
    if set(found) != {(r["job_id"], r["attempt_id"]) for r in rows}:
        raise WorkerAuthorityError("legacy adoption inventory differs; preserve store")
    snapshot = {}
    for row in rows:
        job_id, attempt_id = row["job_id"], row["attempt_id"]
        jobdir, request, state = _load(root, job_id, attempt_id, parent=parent,
            expected_runtime_root=expected_runtime_root, allow_legacy_readonly=True)
        if "worker_store_id" in request:
            raise WorkerAuthorityError("bound job requires original store authority; cannot adopt")
        if state["status"] != "completed" or _pid_alive(state.get("pid")):
            raise WorkerAuthorityError("legacy job is not completed and inactive; cannot adopt")
        description = fetch_description(worker_root=root, job_id=job_id, attempt_id=attempt_id,
            allowed_runtime_parent=parent, expected_runtime_root=expected_runtime_root)
        files = _adoption_files(jobdir)
        if files != row["files"]:
            raise StateIntegrityError("legacy adoption file inventory changed")
        receipts = {}
        for role, suffix in (("controller_assignment", ".json"), ("controller_result", ".imported.json")):
            evidence = row[role]
            path = Path(evidence["path"])
            if (path.is_relative_to(root) or path.parent.name != "overflow-dispatch"
                    or path.name != job_id + suffix):
                raise PathValidationError("legacy controller receipt path is unsafe")
            payload = _adoption_bytes(path)
            digest = _sha(payload)
            if digest != evidence["sha256"]:
                raise ResultIntegrityError("legacy controller receipt changed")
            receipts[str(path)] = digest
            if role == "controller_result":
                if digest != description["result_sha256"] or len(payload) != description["result_bytes"]:
                    raise ResultIntegrityError("controller receipt differs from original worker result")
            else:
                try:
                    assignment = json.loads(payload)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise StateIntegrityError("controller assignment is invalid JSON") from exc
                fields = ("job_id", "attempt_id", "release_sha256", "input_sha256", "lane", "date", "item")
                if (not isinstance(assignment, dict)
                        or any(assignment.get(k) != request[k] for k in fields)
                        or assignment.get("profile_binding") != request.get("profile_binding")):
                    raise StateIntegrityError("controller assignment differs from legacy request")
                authority, saved = assignment.get("worker_authority"), assignment.get("request")
                if any(v is not None and not isinstance(v, dict) for v in (authority, saved)):
                    raise StateIntegrityError("legacy controller authority is invalid")
                if ("worker_store_id" in (authority or {})
                        or "worker_store_id" in (saved or {})):
                    raise WorkerAuthorityError("controller has bound authority; restore it instead of adopting")
        snapshot[job_id + "/" + attempt_id] = {"files": files, "receipts": receipts}
    return snapshot


def adopt_legacy_store(
    inventory: Mapping[str, Any], *, worker_root: str | os.PathLike[str],
    allowed_runtime_parent: str | os.PathLike[str],
    expected_runtime_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Explicitly establish future authority beside fully receipted old jobs.

    The caller supplies hash-bound copies of original controller receipts.
    This is not receipt collection/authentication, result promotion, or a job
    migration. Only the existing store marker is written; old jobs remain
    unbound and cannot execute. Exact retries verify the same inventory.
    """
    root = Path(worker_root)
    if not root.is_absolute() or root.resolve() != root:
        raise PathValidationError("legacy adoption root is unsafe")
    parent = _abs(str(allowed_runtime_parent), "allowed_runtime_parent")
    if (not isinstance(inventory, Mapping)
            or set(inventory) != {"schema_version", "worker_root", "jobs"}
            or inventory["schema_version"] != LEGACY_ADOPTION_SCHEMA
            or inventory["worker_root"] != str(root)
            or not isinstance(inventory["jobs"], list) or not 1 <= len(inventory["jobs"]) <= 50):
        raise RequestValidationError("legacy adoption inventory is invalid")
    inventory = json.loads(_json_bytes(dict(inventory)))
    rows = inventory["jobs"]
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) != {"job_id", "attempt_id", "files", "controller_assignment", "controller_result"}
                or not isinstance(row["job_id"], str) or not _HEX64.fullmatch(row["job_id"])
                or not isinstance(row["attempt_id"], str) or not _HEX32.fullmatch(row["attempt_id"])
                or not isinstance(row["files"], dict) or not row["files"]
                or any(not isinstance(k, str) or not isinstance(v, str) or not _HEX64.fullmatch(v)
                       for k, v in row["files"].items())):
            raise RequestValidationError("legacy adoption job inventory is invalid")
        for role in ("controller_assignment", "controller_result"):
            evidence = row[role]
            if (not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}
                    or not isinstance(evidence["path"], str) or not Path(evidence["path"]).is_absolute()
                    or not isinstance(evidence["sha256"], str) or not _HEX64.fullmatch(evidence["sha256"])):
                raise RequestValidationError("legacy adoption receipt is invalid")
    if len({(r["job_id"], r["attempt_id"]) for r in rows}) != len(rows):
        raise RequestValidationError("duplicate legacy adoption attempt")
    _directory(root, create=False)
    _directory(root / "jobs", create=False)
    digest = _sha(_json_bytes(dict(inventory)))
    with _slot(root):
        store_id = _store_id(root)
        marker = root / "worker-store.json"
        before_marker = _adoption_bytes(marker) if store_id is not None else None
        if store_id is not None and json.loads(before_marker).get("legacy_adoption_sha256") != digest:
            raise WorkerAuthorityError("store already has different authority; cannot adopt")
        before = _adoption_snapshot(root, rows, parent, expected_runtime_root)
        if before != _adoption_snapshot(root, rows, parent, expected_runtime_root):
            raise StateIntegrityError("legacy adoption input drifted; preserve store")
        if store_id is None:
            store_id = uuid.uuid4().hex
            _write(marker, {"schema_version": "autoslice-worker-store.v1", "store_id": store_id,
                            "legacy_adoption_sha256": digest}, exclusive=True)
        elif _adoption_bytes(marker) != before_marker:
            raise WorkerAuthorityError("store authority changed during adoption retry")
    return {"ok": True, "worker_root": str(root), "worker_store_id": store_id,
            "legacy_adoption_sha256": digest, "legacy_attempts": len(rows),
            "already_adopted": before_marker is not None}


def _require_store(request: Mapping[str, Any], root: Path) -> str:
    expected = request.get("worker_store_id")
    observed = _store_id(root)
    if expected is None or observed is None or expected != observed:
        raise WorkerAuthorityError("worker store binding missing or changed; preserve original job")
    return observed


def _paths(worker_root: str | os.PathLike[str], job_id: str, attempt_id: str) -> tuple[Path, Path]:
    root = Path(worker_root)
    if not root.is_absolute():
        raise PathValidationError("worker_root is not absolute")
    _directory(root)
    jobs = root / "jobs"
    _directory(jobs)
    if not _HEX64.fullmatch(job_id) or not _HEX32.fullmatch(attempt_id):
        raise PathValidationError("job identity is invalid")
    return jobs / job_id, jobs / job_id / attempt_id


@contextlib.contextmanager
def _slot(root: Path, fd: int | None = None) -> Iterator[Any]:
    jobs = root / "jobs"
    _directory(jobs)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise SlotBusyError("worker slot is busy") from exc
            raise OverflowWorkerError("worker slot cannot be acquired") from exc
        try:
            yield fd
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
        return
    path = jobs / ".worker-slot.lock"
    if path.exists():
        _regular(path)
    try:
        handle = open(path, "a+b", buffering=0)
    except OSError as exc:
        raise OverflowWorkerError("worker slot cannot be opened") from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise SlotBusyError("worker slot is busy") from exc
            raise OverflowWorkerError("worker slot cannot be acquired") from exc
        yield handle
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _state_write(jobdir: Path, state: Mapping[str, Any], expected: int | None = None) -> dict[str, Any]:
    path = jobdir / "state.json"
    if expected is not None:
        old = _read(path, "state")
        if not isinstance(old, dict) or old.get("revision") != expected:
            raise StateIntegrityError("stale state update refused")
    updated = dict(state)
    updated["revision"] = int(updated.get("revision", 0)) + 1
    _write(path, updated)
    return updated


def _retryable_startup_failure(jobdir: Path, state: Mapping[str, Any]) -> bool:
    """Whether this exact attempt can be safely admitted again.

    The worker may only restart a failed attempt when the runtime gate or the
    explicit producer import failed, the child is gone, and no result/bundle
    was materialized. A late producer-side prerequisite failure is deliberately
    excluded even though it has the same public error code.
    """
    if (
        state.get("status") != "failed"
        or state.get("error_code") != RuntimePrerequisiteError.code
        or state.get("startup_stage") not in {_STARTUP_PRE_EXECUTOR, _STARTUP_PRODUCER_IMPORT}
        or _pid_alive(state.get("pid"))
        or state.get("result_sha256") is not None
    ):
        return False
    result_path = jobdir / "result.json"
    try:
        _regular(result_path, absent_ok=True)
    except Exception:
        # Any malformed or unsafe result is evidence, not permission to rerun.
        return False
    bundle = jobdir / "bundle"
    return not result_path.exists() and not bundle.exists() and not bundle.is_symlink()


def _load(
    root: str | os.PathLike[str], job_id: str, attempt_id: str, *,
    parent: str | os.PathLike[str] | None = None,
    expected_runtime_root: str | os.PathLike[str] | None = None,
    allow_legacy_readonly: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    _jobroot, jobdir = _paths(root, job_id, attempt_id)
    _directory(jobdir, create=False)
    request = validate_request(_read(jobdir / "request.json", "request"),
                               allowed_runtime_parent=parent,
                               expected_runtime_root=expected_runtime_root,
                               allow_legacy_readonly=allow_legacy_readonly)
    if "worker_store_id" in request:
        _require_store(request, Path(root))
    state = _read(jobdir / "state.json", "state")
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA:
        raise StateIntegrityError("state is invalid")
    if (request["job_id"], request["attempt_id"]) != (job_id, attempt_id):
        raise StateIntegrityError("request path identity mismatch")
    if any(state.get(field) != request[field] for field in ("job_id", "attempt_id", "release_sha256", "input_sha256")):
        raise StateIntegrityError("state identity mismatch")
    if state.get("request_sha256") != request_sha256(request) or state.get("status") not in _ACTIVE | _TERMINAL:
        raise StateIntegrityError("state identity or status is invalid")
    return jobdir, request, state


def _free(runtime_root: str) -> int:
    path = Path(runtime_root)
    if not path.is_dir():
        raise PathValidationError("runtime_root is not a directory")
    try:
        return int(shutil.disk_usage(path).free)
    except OSError as exc:
        raise PathValidationError("runtime filesystem cannot be inspected") from exc


def _reserve(request: Mapping[str, Any]) -> int:
    free = _free(str(request["runtime_root"]))
    required = request["estimated_peak_bytes"] + request["operational_reserve_bytes"]
    if free <= required:
        raise InsufficientDiskError("runtime filesystem reservation is unavailable")
    return free


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, OverflowWorkerError):
        return exc.code
    if isinstance(exc, ValueError) and str(exc).startswith("RUNTIME_PREREQUISITE:"):
        # Producer-side dependency checks use the same stable prefix even
        # though their public exception class belongs to producer_request.
        return RuntimePrerequisiteError.code
    if isinstance(exc, ImportError):
        return "adapter_unavailable"
    return "execution_failed"


def worker_repository(runtime: Path, release: str | None = None) -> Path:
    own = Path(__file__).resolve().parents[2]
    if own.parent.parent == runtime / "versions":
        if release is not None and own.parent.name != release:
            raise RuntimePrerequisiteError("worker code differs from requested release")
        return own
    version = runtime / "versions" / release / "repo" if release else None
    return version if version is not None and version.is_dir() else (runtime / "repo").resolve()


def validate_runtime_prerequisites(
    runtime_root: str | os.PathLike[str],
    expected_release: str,
    *,
    controller_root: str | os.PathLike[str] | None = None,
    check_adapter: bool = True,
    lane: str | None = None,
    expected_profile_binding: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Validate only worker startup identity, before source/producer work.

    Source media and producer inputs deliberately stay out of this gate.  The
    release seal is the runtime's code/assets identity; adapter import is the
    remaining non-producing startup check.  Callers use the same function for
    probe, admission, and the child defense-in-depth check so a valid marker
    cannot make an unstartable worker eligible.
    """
    try:
        runtime_text = _abs(str(runtime_root), "runtime_root")
        controller_text = _abs(
            str(runtime_root if controller_root is None else controller_root),
            "controller_root",
        )
        runtime = Path(runtime_text)
        controller = Path(controller_text)
        if runtime != controller:
            raise RuntimePrerequisiteError("runtime_root does not match controller_root")
        _directory(runtime, create=False)
        repo = worker_repository(runtime, expected_release)
        _directory(repo, create=False)
        if not isinstance(expected_release, str) or not _HEX64.fullmatch(expected_release):
            raise RuntimePrerequisiteError("runtime release marker is missing or invalid")
        from src.autoslice.overflow_release import verify

        verify(repo, expected_release)
        if expected_profile_binding is not None:
            pin_request_profile({"runtime_root": str(runtime), "profile_binding": expected_profile_binding})
    except RuntimePrerequisiteError:
        raise
    except Exception as exc:
        raise RuntimePrerequisiteError("runtime release verification failed") from exc
    if check_adapter:
        try:
            _adapter()
        except RuntimePrerequisiteError:
            raise
        except Exception as exc:
            raise RuntimePrerequisiteError("overflow execution adapter import failed") from exc
    try:
        # This is intentionally the same typed checker used by the ordinary
        # producer request path.  A probe has no lane-specific request, so it
        # checks common media tools under uniform-host talk defaults; submit
        # and run pass the selected lane and therefore check only that lane's
        # selected local dependency.
        from src.autoslice.producer_request import validate_producer_runtime_prerequisites

        selected_lane = "talk" if lane is None else lane
        speaker_mode = "uniform_host" if lane is None else os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
        speaker_python = os.environ.get("AUTOSLICE_SPEAKER_PYTHON")
        host_vocal_python = os.environ.get("AUTOSLICE_HOST_VOCAL_PYTHON")
        default_python = str(runtime / "venv-diar/bin/python")
        validate_producer_runtime_prerequisites(
            host="localhost", speaker_mode=speaker_mode,
            speaker_python=speaker_python or default_python,
            lane=selected_lane,
            host_vocal_python=host_vocal_python or default_python,
            check_media=True,
        )
        from src.autoslice.native_audio_runtime import native_audio_readiness
        if not all(row['ready'] for row in native_audio_readiness(runtime)):
            raise RuntimePrerequisiteError('native_audio: provider runtime configuration not ready')
    except RuntimePrerequisiteError:
        raise
    except Exception as exc:
        detail = str(exc)
        raise RuntimePrerequisiteError(detail or "producer runtime prerequisite failed") from exc
    return {"runtime_root": str(runtime), "release_sha256": expected_release}


def _script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "autoslice_overflow_worker.py"


def pin_request_profile(request: Mapping[str, Any]):
    """Bind task selection before any upstream producer module can import."""
    from src.autoslice.channel_profile import (
        ChannelProfileError, PROFILE_ENV, PROFILE_MANIFEST_ENV, PROFILE_BINDING_ENV,
        load_channel_profile, normalize_profile_binding, profile_environment,
    )
    try:
        binding = normalize_profile_binding(request.get("profile_binding"))
        profile = load_channel_profile(worker_repository(Path(request["runtime_root"]), request.get("release_sha256")), profile_binding=binding)
        env = profile_environment(profile, {})
        for name in (PROFILE_ENV, PROFILE_MANIFEST_ENV, PROFILE_BINDING_ENV):
            os.environ[name] = env[name]
        return profile
    except (ChannelProfileError, OSError, ValueError) as exc:
        raise RuntimePrerequisiteError(f"channel_profile: {exc}", startup_stage=_STARTUP_PRE_EXECUTOR) from exc


def _spawn(root: Path, jobdir: Path, request: Mapping[str, Any], parent: str, slot: Any,
           popen: Callable[..., Any]) -> tuple[Any, int]:
    gate_read, gate_write = os.pipe()
    command = [sys.executable, str(_script()), "--worker-root", str(root),
               "--runtime-root", request["runtime_root"],
               "--allowed-runtime-parent", parent, "run", "--job-id",
               request["job_id"], "--attempt-id", request["attempt_id"],
               "--slot-fd", str(slot.fileno()),
               "--start-fd", str(gate_read)]
    log_path = jobdir / "worker.log"
    if log_path.exists():
        _regular(log_path)
    try:
        log = open(log_path, "ab", buffering=0)
        try:
            process = popen(command, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, close_fds=True,
                            pass_fds=(slot.fileno(), gate_read), start_new_session=True)
        finally:
            log.close()
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(gate_read)
        with contextlib.suppress(OSError):
            os.close(gate_write)
        raise
    with contextlib.suppress(OSError):
        os.close(gate_read)
    return process, gate_write


def _launch_submitted(
    *, root: Path, jobdir: Path, request: Mapping[str, Any], parent: str,
    slot: Any, state: Mapping[str, Any], popen_factory: Callable[..., Any],
) -> dict[str, Any]:
    """Spawn one submitted attempt after its caller has run the startup gate."""
    process = None
    gate_write: int | None = None
    state = dict(state)
    try:
        process, gate_write = _spawn(root, jobdir, request, parent, slot, popen_factory)
        state["pid"] = int(process.pid)
        state = _state_write(jobdir, state, expected=state["revision"])
        try:
            os.write(gate_write, b"1")
        except BrokenPipeError:
            # A test/in-process launcher may intentionally not retain the
            # inherited gate descriptor.  A real Popen child closing it before
            # admission is a spawn failure and remains fail-closed.
            if popen_factory is subprocess.Popen:
                raise
        os.close(gate_write)
        gate_write = None
    except BaseException as exc:
        if gate_write is not None:
            with contextlib.suppress(OSError):
                os.write(gate_write, b"0")
                os.close(gate_write)
        if process is not None:
            with contextlib.suppress(OSError):
                terminate = getattr(process, "terminate", None)
                if callable(terminate):
                    terminate()
        failed = dict(state)
        failed.update(status="failed", finished_at=_now(), error_code=_error_code(exc))
        with contextlib.suppress(BaseException):
            _state_write(jobdir, failed, expected=state["revision"])
        raise exc if isinstance(exc, OverflowWorkerError) else OverflowWorkerError("worker spawn failed") from exc
    return state


def _result(jobdir: Path) -> tuple[str, int]:
    path = jobdir / "result.json"
    _regular(path)
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultIntegrityError("result is invalid JSON") from exc
    try:
        _json_bytes(value)
    except RequestValidationError as exc:
        raise ResultIntegrityError("result is not safe JSON") from exc
    return _sha(payload), len(payload)


def _bundle(jobdir: Path) -> tuple[str | None, int | None]:
    path = jobdir / "bundle"
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_dir():
        raise ResultIntegrityError("bundle is unsafe")
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        base = Path(current)
        for name in dirs:
            if (base / name).is_symlink() or not (base / name).is_dir():
                raise ResultIntegrityError("bundle directory is unsafe")
        for name in files:
            child = base / name
            _regular(child)
            total += child.stat().st_size
    return str(path), total


def submit_request(
    request: Mapping[str, Any], *,
    worker_root: str | os.PathLike[str] = DEFAULT_WORKER_ROOT,
    allowed_runtime_parent: str | os.PathLike[str],
    expected_runtime_root: str | os.PathLike[str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    parent = _abs(str(allowed_runtime_parent), "allowed_runtime_parent")
    request = validate_request(request, allowed_runtime_parent=parent,
                               expected_runtime_root=expected_runtime_root)
    root = Path(worker_root)
    if not root.is_absolute():
        raise PathValidationError("worker_root is not absolute")
    _directory(root)
    jobs = root / "jobs"
    _directory(jobs)
    with _slot(root) as slot:
        _require_store(request, root)
        jobroot = jobs / request["job_id"]
        if jobroot.exists():
            _directory(jobroot, create=False)
            attempts = [p for p in jobroot.iterdir() if not p.name.startswith(".")]
            for attempt in attempts:
                if attempt.is_symlink() or not attempt.is_dir() or not _HEX32.fullmatch(attempt.name):
                    raise StateIntegrityError("job attempt directory is unsafe")
                old_request = validate_request(_read(attempt / "request.json", "request"),
                                               allowed_runtime_parent=parent,
                                               expected_runtime_root=expected_runtime_root)
                if request_sha256({k: v for k, v in old_request.items() if k != "attempt_id"}) != request_sha256(
                    {k: v for k, v in request.items() if k != "attempt_id"}
                ):
                    raise JobConflictError("job identity already has a different request")
                if attempt.name != request["attempt_id"]:
                    raise DuplicateJobError("job identity already has an execution")
                state = _read(attempt / "state.json", "state")
                if not isinstance(state, dict):
                    raise StateIntegrityError("existing state is invalid")
                if _retryable_startup_failure(attempt, state):
                    free = _reserve(request)
                    # A failed startup attempt is retried only after the
                    # same sealed runtime gate passes again.  The job and
                    # attempt identity remain unchanged.
                    validate_runtime_prerequisites(
                        request["runtime_root"], request["release_sha256"],
                        controller_root=request["controller_root"], lane=request["lane"],
                        expected_profile_binding=request["profile_binding"],
                    )
                    resumed = dict(state)
                    resumed.update(
                        status="submitted", pid=None, error_code=None,
                        startup_stage=_STARTUP_PRE_EXECUTOR,
                        runtime_free_bytes_at_admission=free,
                    )
                    for field in (
                        "started_at", "finished_at", "runtime_free_bytes_at_start",
                        "result_sha256", "result_bytes", "result_path", "bundle_path",
                        "bundle_bytes",
                    ):
                        resumed.pop(field, None)
                    resumed = _state_write(attempt, resumed, expected=state["revision"])
                    resumed = _launch_submitted(
                        root=root, jobdir=attempt, request=request, parent=parent,
                        slot=slot, state=resumed, popen_factory=popen_factory,
                    )
                    return _admission(True, False, root, attempt, resumed)
                return _admission(False, True, root, attempt, state)
        free = _reserve(request)
        # Do this before creating the durable job/assignment.  A valid marker
        # and free disk are not enough to admit a worker whose sealed runtime
        # or adapter cannot start.  Existing assignments are handled by the
        # backend and remain durable for recovery.
        validate_runtime_prerequisites(
            request["runtime_root"], request["release_sha256"],
            controller_root=request["controller_root"], lane=request["lane"],
            expected_profile_binding=request["profile_binding"],
        )
        _directory(jobroot)
        jobdir = jobroot / request["attempt_id"]
        try:
            jobdir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise JobConflictError("job attempt already exists") from exc
        _sync_dir(jobroot)
        _write(jobdir / "request.json", request, exclusive=True)
        state = _state_write(jobdir, {
            "schema_version": STATE_SCHEMA, "job_id": request["job_id"],
            "attempt_id": request["attempt_id"], "request_sha256": request_sha256(request),
            "release_sha256": request["release_sha256"], "input_sha256": request["input_sha256"],
            "lane": request["lane"], "date": request["date"], "status": "submitted",
            "pid": None, "created_at": _now(), "runtime_free_bytes_at_admission": free,
            "estimated_peak_bytes": request["estimated_peak_bytes"],
            "operational_reserve_bytes": request["operational_reserve_bytes"],
            "result_sha256": None, "result_bytes": None, "error_code": None,
            "startup_stage": _STARTUP_PRE_EXECUTOR, "revision": 0,
        })
        state = _launch_submitted(
            root=root, jobdir=jobdir, request=request, parent=parent,
            slot=slot, state=state, popen_factory=popen_factory,
        )
        return _admission(True, False, root, jobdir, state)


def _admission(accepted: bool, duplicate: bool, root: Path, jobdir: Path,
               state: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "accepted": accepted, "duplicate": duplicate,
            "job_id": state["job_id"], "attempt_id": state["attempt_id"],
            "status": state["status"], "pid": state.get("pid"),
            "jobdir": str(jobdir), "worker_root": str(root),
            "worker_store_id": _store_id(root)}


def _gate(fd: int) -> bool:
    try:
        return os.read(fd, 1) == b"1"
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _adapter() -> Callable[[dict[str, Any], Path], Any]:
    try:
        func = getattr(importlib.import_module("src.autoslice.overflow_execution_worker"), "execute_request")
    except (ImportError, AttributeError) as exc:
        raise OverflowWorkerError("overflow execution adapter is unavailable") from exc
    if not callable(func):
        raise OverflowWorkerError("overflow execution adapter is invalid")
    return func


def run_job(
    *, worker_root: str | os.PathLike[str], job_id: str, attempt_id: str,
    allowed_runtime_parent: str | os.PathLike[str], slot_fd: int | None = None,
    start_fd: int | None = None, executor: Callable[[dict[str, Any], Path], Any] | None = None,
    expected_runtime_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    parent = _abs(str(allowed_runtime_parent), "allowed_runtime_parent")
    root = Path(worker_root)
    if not root.is_absolute():
        raise PathValidationError("worker_root is not absolute")
    _directory(root)
    if start_fd is not None and not _gate(start_fd):
        return {"ok": False, "status": "aborted", "job_id": job_id, "attempt_id": attempt_id}
    with _slot(root, fd=slot_fd) if slot_fd is not None else _slot(root):
        jobdir, request, state = _load(root, job_id, attempt_id, parent=parent,
                                       expected_runtime_root=expected_runtime_root)
        _require_store(request, root)
        if state["status"] in _TERMINAL:
            return {"ok": True, "status": state["status"], "job_id": job_id, "attempt_id": attempt_id}
        if state["status"] != "submitted":
            return {"ok": True, "status": "unknown", "lifecycle_status": state["status"],
                    "job_id": job_id, "attempt_id": attempt_id}
        try:
            try:
                validate_runtime_prerequisites(
                    request["runtime_root"], request["release_sha256"],
                    controller_root=request["controller_root"], lane=request["lane"],
                    expected_profile_binding=request["profile_binding"],
                )
            except RuntimePrerequisiteError as exc:
                failed = dict(state)
                failed.update(
                    status="failed", finished_at=_now(), error_code=exc.code,
                    startup_stage=_STARTUP_PRE_EXECUTOR,
                )
                _state_write(jobdir, failed, expected=state["revision"])
                return {"ok": False, "status": "failed", "job_id": job_id,
                        "attempt_id": attempt_id, "error_code": failed["error_code"]}
            free = _reserve(request)
            result_path = jobdir / "result.json"
            if result_path.exists():
                raise ResultIntegrityError("result already exists")
            running = dict(state)
            running.update(status="running", pid=os.getpid(), started_at=_now(),
                           runtime_free_bytes_at_start=free,
                           startup_stage=_STARTUP_EXECUTOR_STARTED)
            running = _state_write(jobdir, running, expected=state["revision"])
            try:
                result = (executor or _adapter())(request, jobdir)
                if result is not None:
                    from src.autoslice.channel_profile import require_profile_binding
                    require_profile_binding(result.get("profile_binding"), request["profile_binding"])
                    _write(result_path, result, exclusive=True)
                from src.autoslice.channel_profile import require_profile_binding
                stored_result = _read(result_path, "result")
                require_profile_binding(stored_result.get("profile_binding"), request["profile_binding"])
                result_sha, result_bytes = _result(jobdir)
                bundle_path, bundle_bytes = _bundle(jobdir)
            except BaseException as exc:
                failed = dict(running)
                failed.update(status="failed", finished_at=_now(), error_code=_error_code(exc))
                if isinstance(exc, RuntimePrerequisiteError) and exc.startup_stage == _STARTUP_PRODUCER_IMPORT:
                    failed["startup_stage"] = _STARTUP_PRODUCER_IMPORT
                _state_write(jobdir, failed, expected=running["revision"])
                return {"ok": False, "status": "failed", "job_id": job_id,
                        "attempt_id": attempt_id, "error_code": failed["error_code"]}
            completed = dict(running)
            completed.update(status="completed", finished_at=_now(), result_sha256=result_sha,
                             result_bytes=result_bytes, result_path="result.json",
                             bundle_path=bundle_path, bundle_bytes=bundle_bytes, error_code=None)
            _state_write(jobdir, completed, expected=running["revision"])
            return {"ok": True, "status": "completed", "job_id": job_id,
                    "attempt_id": attempt_id, "result_sha256": result_sha,
                    "result_bytes": result_bytes, "bundle_path": bundle_path,
                    "bundle_bytes": bundle_bytes}
        except BaseException as exc:
            current = _read(jobdir / "state.json", "state")
            if isinstance(current, dict) and current.get("status") == "submitted":
                failed = dict(current)
                failed.update(status="failed", finished_at=_now(), error_code=_error_code(exc))
                with contextlib.suppress(BaseException):
                    _state_write(jobdir, failed, expected=current["revision"])
            raise


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def status_job(
    *, worker_root: str | os.PathLike[str], job_id: str, attempt_id: str,
    allowed_runtime_parent: str | os.PathLike[str] | None = None,
    expected_runtime_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if allowed_runtime_parent is None:
        raise PathValidationError("allowed runtime parent is required")
    try:
        jobdir, request, state = _load(worker_root, job_id, attempt_id,
                                       parent=allowed_runtime_parent,
                                       expected_runtime_root=expected_runtime_root,
                                       allow_legacy_readonly=True)
    except JobNotFoundError:
        return {"ok": True, "status": "absent", "job_id": job_id, "attempt_id": attempt_id,
                "worker_store_id": _store_id(Path(worker_root))}
    lifecycle = state["status"]
    alive = _pid_alive(state.get("pid"))
    observed = lifecycle if lifecycle in _TERMINAL or alive else "unknown"
    result_present = False
    try:
        _regular(jobdir / "result.json", absent_ok=True)
        result_present = (jobdir / "result.json").exists()
    except Exception:
        # An unsafe result is conservatively reported as present.  The
        # controller must never use a malformed artifact as retry permission.
        result_present = True
    result: dict[str, Any] = {"ok": True, "status": observed, "lifecycle_status": lifecycle,
        "worker_store_id": _store_id(Path(worker_root)),
        "process_alive": alive, "job_id": job_id, "attempt_id": attempt_id,
        "pid": state.get("pid"), "jobdir": str(jobdir), "release_sha256": request["release_sha256"],
        "input_sha256": request["input_sha256"], "lane": request["lane"], "date": request["date"],
        "revision": state.get("revision"), "startup_stage": state.get("startup_stage"),
        "result_present": result_present,
        "bundle_present": (jobdir / "bundle").exists() or (jobdir / "bundle").is_symlink()}
    if observed == "unknown":
        result["reason"] = "unfinished_process_not_live"
    if lifecycle == "completed":
        result.update(result_sha256=state.get("result_sha256"), result_bytes=state.get("result_bytes"))
        if state.get("bundle_path") is not None:
            result.update(bundle_path=state.get("bundle_path"), bundle_bytes=state.get("bundle_bytes"))
    if lifecycle == "failed":
        result["error_code"] = state.get("error_code")
    return result


def fetch_description(
    *, worker_root: str | os.PathLike[str], job_id: str, attempt_id: str,
    allowed_runtime_parent: str | os.PathLike[str] | None = None,
    expected_runtime_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if allowed_runtime_parent is None:
        raise PathValidationError("allowed runtime parent is required")
    jobdir, _request, state = _load(worker_root, job_id, attempt_id,
                                    parent=allowed_runtime_parent,
                                    expected_runtime_root=expected_runtime_root,
                                    allow_legacy_readonly=True)
    if state["status"] != "completed":
        raise JobUnknownError("job has no completed result")
    result_sha, result_bytes = _result(jobdir)
    if (result_sha, result_bytes) != (state.get("result_sha256"), state.get("result_bytes")):
        raise ResultIntegrityError("result hash does not match state")
    bundle_path, bundle_bytes = _bundle(jobdir)
    if (bundle_path, bundle_bytes) != (state.get("bundle_path"), state.get("bundle_bytes")):
        raise ResultIntegrityError("bundle metadata does not match state")
    answer: dict[str, Any] = {"ok": True, "status": "completed", "job_id": job_id,
        "attempt_id": attempt_id, "result_path": str(jobdir / "result.json"),
        "result_sha256": result_sha, "result_bytes": result_bytes}
    if bundle_path is not None:
        answer.update(bundle_path=bundle_path, bundle_bytes=bundle_bytes)
    return answer


def _marker(path: Path) -> tuple[str | None, str]:
    try:
        _regular(path, absent_ok=True)
        if not path.exists():
            return None, "missing"
        value = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, UnicodeError, IndexError, StateIntegrityError):
        return None, "invalid"
    return (value, "valid") if _HEX64.fullmatch(value) else (None, "invalid")


def probe(*, worker_root: str | os.PathLike[str] = DEFAULT_WORKER_ROOT,
          runtime_root: str | os.PathLike[str] | None = None,
          release_marker: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    root = Path(worker_root)
    if not root.is_absolute():
        raise PathValidationError("worker_root is not absolute")
    _directory(root)
    if runtime_root is None:
        configured = os.environ.get("AUTOSLICE_OVERFLOW_RUNTIME_ROOT")
        candidate = Path(configured) if configured else Path("/opt/bilive/autoslice")
        runtime = candidate if candidate.is_dir() else root
    else:
        runtime = Path(runtime_root)
    if not runtime.is_absolute():
        raise PathValidationError("runtime_root is not absolute")
    try:
        available = _free(str(runtime))
    except OverflowWorkerError:
        available = None
    if release_marker is not None:
        marker = Path(release_marker)
    elif os.environ.get("AUTOSLICE_RELEASE_SHA256_FILE"):
        marker = Path(os.environ["AUTOSLICE_RELEASE_SHA256_FILE"])
    else:
        candidates = (worker_repository(runtime) / "OVERFLOW_RELEASE_SHA256", root / "release.sha256")
        marker = next((p for p in candidates if p.exists()), candidates[0])
    if not marker.is_absolute():
        raise PathValidationError("release marker is not absolute")
    release, marker_status = _marker(marker)
    startup_ok = False
    startup_error_code = "runtime_prerequisite_failed"
    if release is not None:
        try:
            validate_runtime_prerequisites(str(runtime), release)
        except RuntimePrerequisiteError as exc:
            startup_error_code = exc.code
        else:
            startup_ok = True
    free = True
    try:
        with _slot(root):
            store_id = _store_id(root, initialize=True)
    except SlotBusyError:
        free = False
        store_id = _store_id(root)
    return {"ok": True, "worker_root": str(root), "runtime_root": str(runtime),
            "worker_store_id": store_id,
            "available_bytes": available, "slot_free": free, "slot_busy": not free,
            "slot_status": "free" if free else "busy", "python": sys.executable,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "release_sha256": release, "release_marker_status": marker_status,
            "startup_ok": startup_ok, "startup_error_code": None if startup_ok else startup_error_code}


__all__ = ["DEFAULT_WORKER_ROOT", "DuplicateJobError", "InsufficientDiskError",
           "JobConflictError", "JobNotFoundError", "JobUnknownError", "OverflowWorkerError",
           "PathValidationError", "REQUEST_SCHEMA", "RequestValidationError",
           "ResultIntegrityError", "SlotBusyError", "StateIntegrityError", "compute_job_id",
           "RuntimePrerequisiteError", "fetch_description", "new_attempt_id", "probe",
           "request_identity_payload", "request_sha256", "run_job", "status_job",
           "submit_request", "validate_request", "validate_runtime_prerequisites",
           "LEGACY_ADOPTION_SCHEMA", "adopt_legacy_store"]
