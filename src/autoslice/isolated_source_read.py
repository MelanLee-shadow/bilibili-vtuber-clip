"""Bound FUSE-backed reads outside the long-lived autoslice runner process."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


RESULT_SCHEMA = "autoslice-isolated-source-read-result.v1"
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_ACTIVE_READS = 4
MAX_REGISTRY_LOCKS = 64
_LOCAL_FILES = {
    "marker.json",
    "marker.tmp",
    "payload.bin",
    "payload.part",
    "result.json",
    "result.tmp",
}
_TIMED_OUT_CHILDREN: list[subprocess.Popen[bytes]] = []


@dataclass(frozen=True)
class IsolatedSourceRead:
    payload: bytes
    source_binding: dict[str, object]


class IsolatedSourceReadError(RuntimeError):
    def __init__(self, reason_code: str, evidence: Mapping[str, object]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.evidence = dict(evidence)


class _ChildReadError(RuntimeError):
    def __init__(self, reason_code: str, error_type: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.error_type = error_type


def _source_key(source_path: str) -> str:
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()


def _evidence(source_path: str, reason_code: str, **extra: object) -> dict[str, object]:
    return {
        "source_path": source_path,
        "source_key": _source_key(source_path),
        "reason_code": reason_code,
        **extra,
    }


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise OSError(f"unsafe local spool directory: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise OSError(f"local spool directory is not private: {path}")


def _linux_filesystem_type(path: Path) -> str | None:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.is_file():
        return "local-test-platform" if sys.platform != "linux" else None
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        absolute = str(existing.resolve(strict=True))
    except OSError:
        absolute = str(existing.absolute())
    best: tuple[int, str] | None = None
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = fields[4]
            filesystem_type = fields[separator + 1]
        except (IndexError, ValueError):
            continue
        for encoded, decoded in (
            (r"\040", " "),
            (r"\011", "\t"),
            (r"\012", "\n"),
            (r"\134", "\\"),
        ):
            mount_point = mount_point.replace(encoded, decoded)
        if absolute == mount_point or absolute.startswith(mount_point.rstrip("/") + "/"):
            candidate = (len(mount_point), filesystem_type)
            if best is None or candidate[0] > best[0]:
                best = candidate
    return best[1] if best else None


def _assert_local_spool(path: Path) -> None:
    filesystem_type = _linux_filesystem_type(path)
    if filesystem_type is None:
        raise IsolatedSourceReadError(
            "LOCAL_SPOOL_FILESYSTEM_UNKNOWN",
            {"spool_root": str(path)},
        )
    if filesystem_type and filesystem_type.lower().startswith("fuse"):
        raise IsolatedSourceReadError(
            "LOCAL_SPOOL_ON_FUSE",
            {"spool_root": str(path), "filesystem_type": filesystem_type},
        )


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise OSError(f"unsafe local spool lock: {path}")
    return descriptor


def _clear_run_directory(run_dir: Path) -> None:
    if not run_dir.exists():
        return
    info = run_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise OSError(f"unsafe local run directory: {run_dir}")
    entries = list(run_dir.iterdir())
    if any(entry.name not in _LOCAL_FILES or entry.is_dir() for entry in entries):
        raise OSError(f"unexpected local run entry: {run_dir}")
    for entry in entries:
        entry.unlink()
    run_dir.rmdir()


def _active_read_count(keys_dir: Path, runs_dir: Path, *, exclude: Path) -> int:
    lock_paths = sorted(keys_dir.glob("*.lock"))
    # A healthy registry can have at most MAX_ACTIVE_READS locked keys; stale
    # unlocked keys are pruned below.  A larger directory is corruption or
    # tampering, so refuse work instead of scanning/deleting without a bound.
    if len(lock_paths) > MAX_REGISTRY_LOCKS:
        raise IsolatedSourceReadError(
            "SOURCE_READ_REGISTRY_LIMIT",
            {"registry_limit": MAX_REGISTRY_LOCKS},
        )
    active = 0
    for lock_path in lock_paths:
        if lock_path == exclude:
            continue
        descriptor = _open_lock(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            active += 1
            os.close(descriptor)
            continue
        try:
            _clear_run_directory(runs_dir / lock_path.stem)
            lock_path.unlink()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    return active


def _stat_binding(source_path: str, info: os.stat_result) -> dict[str, object]:
    return {
        "path": source_path,
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _valid_source_binding(binding: object, *, source_path: str, payload: bytes) -> bool:
    if not isinstance(binding, dict) or set(binding) != {
        "path",
        "size_bytes",
        "mtime_ns",
        "ctime_ns",
        "device",
        "inode",
        "mode",
        "sha256",
    }:
        return False
    integer_fields = ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode", "mode")
    if any(type(binding.get(field)) is not int for field in integer_fields):
        return False
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return bool(
        binding["path"] == source_path
        and binding["size_bytes"] == len(payload)
        and 0 <= binding["mode"] <= 0o7777
        and binding["sha256"] == digest
    )


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name("result.tmp" if path.name == "result.json" else "marker.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _read_local_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > maximum_bytes
        ):
            raise OSError(f"unsafe local result file: {path}")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum_bytes:
                raise OSError(f"oversized local result file: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if size != before.st_size or any(
        getattr(before, field) != getattr(after, field) for field in stable_fields
    ):
        raise OSError(f"local result drifted while reading: {path}")
    return b"".join(chunks)


def _child_copy(source_path: str, run_dir: Path, token: str, lock_fd: int) -> int:
    os.fstat(lock_fd)
    _atomic_json(
        run_dir / "marker.json",
        {
            "schema_version": RESULT_SCHEMA,
            "source_path": source_path,
            "token": token,
            "pid": os.getpid(),
        },
    )
    payload_part = run_dir / "payload.part"
    try:
        before_path = os.lstat(source_path)
        if not stat.S_ISREG(before_path.st_mode):
            raise _ChildReadError("SOURCE_NOT_REGULAR", "NonRegularSource")
        if before_path.st_size > MAX_SOURCE_BYTES:
            raise _ChildReadError("SOURCE_TOO_LARGE", "SourceSizeLimit")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        source_fd = os.open(source_path, flags)
        try:
            before_fd = os.fstat(source_fd)
            before = _stat_binding(source_path, before_fd)
            if before != _stat_binding(source_path, before_path):
                raise _ChildReadError("SOURCE_DRIFT", "SourceIdentityDrift")
            digest = hashlib.sha256()
            size = 0
            with payload_part.open("xb") as destination:
                while chunk := os.read(source_fd, 1024 * 1024):
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise _ChildReadError("SOURCE_TOO_LARGE", "SourceSizeLimit")
                destination.flush()
                os.fsync(destination.fileno())
            after_fd = os.fstat(source_fd)
        finally:
            os.close(source_fd)
        after_path = os.lstat(source_path)
        after = _stat_binding(source_path, after_fd)
        if (
            before != after
            or after != _stat_binding(source_path, after_path)
            or size != after["size_bytes"]
        ):
            raise _ChildReadError("SOURCE_DRIFT", "SourceContentDrift")
        binding = {**after, "sha256": "sha256:" + digest.hexdigest()}
        os.replace(payload_part, run_dir / "payload.bin")
        result: dict[str, object] = {
            "schema_version": RESULT_SCHEMA,
            "status": "OK",
            "token": token,
            "source_path": source_path,
            "source_binding": binding,
        }
        return_code = 0
    except _ChildReadError as exc:
        result = {
            "schema_version": RESULT_SCHEMA,
            "status": "ERROR",
            "token": token,
            "source_path": source_path,
            "reason_code": exc.reason_code,
            "error_type": exc.error_type,
        }
        return_code = 2
    except FileNotFoundError as exc:
        result = {
            "schema_version": RESULT_SCHEMA,
            "status": "ERROR",
            "token": token,
            "source_path": source_path,
            "reason_code": "SOURCE_MISSING",
            "error_type": type(exc).__name__,
        }
        return_code = 2
    except OSError as exc:
        result = {
            "schema_version": RESULT_SCHEMA,
            "status": "ERROR",
            "token": token,
            "source_path": source_path,
            "reason_code": "SOURCE_UNREADABLE",
            "error_type": type(exc).__name__,
        }
        return_code = 2
    payload_part.unlink(missing_ok=True)
    _atomic_json(run_dir / "result.json", result)
    return return_code


def _child_argv(*, source_path: str, run_dir: Path, token: str, lock_fd: int) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        source_path,
        str(run_dir),
        token,
        str(lock_fd),
    ]


def _load_local_result(run_dir: Path, *, source_path: str, token: str) -> IsolatedSourceRead:
    try:
        result_bytes = _read_local_bytes(run_dir / "result.json", maximum_bytes=MAX_RESULT_BYTES)
        document = json.loads(result_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedSourceReadError(
            "SOURCE_READ_CHILD_FAILED",
            _evidence(source_path, "SOURCE_READ_CHILD_FAILED", error_type=type(exc).__name__),
        ) from exc
    if not isinstance(document, dict) or any(
        document.get(key) != value
        for key, value in {
            "schema_version": RESULT_SCHEMA,
            "source_path": source_path,
            "token": token,
        }.items()
    ):
        raise IsolatedSourceReadError(
            "LOCAL_READ_RESULT_INVALID",
            _evidence(source_path, "LOCAL_READ_RESULT_INVALID"),
        )
    if document.get("status") == "ERROR":
        reason_code = str(document.get("reason_code") or "SOURCE_READ_CHILD_FAILED")
        raise IsolatedSourceReadError(
            reason_code,
            _evidence(source_path, reason_code, error_type=document.get("error_type")),
        )
    binding = document.get("source_binding")
    try:
        payload = _read_local_bytes(run_dir / "payload.bin", maximum_bytes=MAX_SOURCE_BYTES)
    except OSError as exc:
        raise IsolatedSourceReadError(
            "LOCAL_READ_RESULT_INVALID",
            _evidence(source_path, "LOCAL_READ_RESULT_INVALID", error_type=type(exc).__name__),
        ) from exc
    if document.get("status") != "OK" or not _valid_source_binding(
        binding, source_path=source_path, payload=payload
    ):
        raise IsolatedSourceReadError(
            "LOCAL_READ_RESULT_INVALID",
            _evidence(source_path, "LOCAL_READ_RESULT_INVALID"),
        )
    return IsolatedSourceRead(payload=payload, source_binding=dict(binding))


def _terminate_without_wait(process: subprocess.Popen[bytes]) -> str | None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except OSError as exc:
        return type(exc).__name__
    return None


def _reap_exited_children_nonblocking() -> None:
    _TIMED_OUT_CHILDREN[:] = [process for process in _TIMED_OUT_CHILDREN if process.poll() is None]


def _cleanup_completed(root: Path, key_path: Path, run_dir: Path, key_fd: int) -> None:
    registry_fd = _open_lock(root / "registry.lock")
    try:
        fcntl.flock(registry_fd, fcntl.LOCK_EX)
        _clear_run_directory(run_dir)
        key_path.unlink(missing_ok=True)
    finally:
        fcntl.flock(registry_fd, fcntl.LOCK_UN)
        os.close(registry_fd)
        os.close(key_fd)


def _local_spool_error(source_path: str, exc: OSError) -> IsolatedSourceReadError:
    return IsolatedSourceReadError(
        "LOCAL_SPOOL_UNAVAILABLE",
        _evidence(source_path, "LOCAL_SPOOL_UNAVAILABLE", error_type=type(exc).__name__),
    )


def read_source_bytes_isolated(
    path: Path | str,
    *,
    timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    spool_root: Path | None = None,
    poll_interval_seconds: float = 0.05,
) -> IsolatedSourceRead:
    """Read source bytes in a killable child; never wait after a timed-out read."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("read timeout and poll interval must be positive")
    # Reap only children that have already exited. A D-state reader remains
    # alive and locked, and this never waits for it.
    _reap_exited_children_nonblocking()
    source_path = os.path.abspath(os.fspath(path))
    root = spool_root or Path(tempfile.gettempdir()) / f"autoslice-source-read-{os.geteuid()}"
    try:
        _assert_local_spool(root)
        keys_dir = root / "keys"
        runs_dir = root / "runs"
        for directory in (root, keys_dir, runs_dir):
            _private_directory(directory)
        _assert_local_spool(root)
    except IsolatedSourceReadError:
        raise
    except OSError as exc:
        raise _local_spool_error(source_path, exc) from exc
    key = _source_key(source_path)
    key_path = keys_dir / f"{key}.lock"
    run_dir = runs_dir / key
    try:
        registry_fd = _open_lock(root / "registry.lock")
    except OSError as exc:
        raise _local_spool_error(source_path, exc) from exc
    key_fd: int | None = None
    try:
        fcntl.flock(registry_fd, fcntl.LOCK_EX)
        key_fd = _open_lock(key_path)
        try:
            fcntl.flock(key_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(key_fd)
            key_fd = None
            raise IsolatedSourceReadError(
                "SOURCE_READ_ALREADY_ACTIVE",
                _evidence(source_path, "SOURCE_READ_ALREADY_ACTIVE"),
            ) from exc
        active = _active_read_count(keys_dir, runs_dir, exclude=key_path)
        if active >= MAX_ACTIVE_READS:
            raise IsolatedSourceReadError(
                "SOURCE_READ_ORPHAN_LIMIT",
                _evidence(
                    source_path,
                    "SOURCE_READ_ORPHAN_LIMIT",
                    active_read_limit=MAX_ACTIVE_READS,
                ),
            )
        _clear_run_directory(run_dir)
        run_dir.mkdir(mode=0o700)
        token = secrets.token_hex(16)
        process = subprocess.Popen(
            _child_argv(
                source_path=source_path,
                run_dir=run_dir,
                token=token,
                lock_fd=key_fd,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(key_fd,),
            start_new_session=True,
        )
    except Exception as exc:
        if key_fd is not None:
            fcntl.flock(key_fd, fcntl.LOCK_UN)
            os.close(key_fd)
            key_path.unlink(missing_ok=True)
            _clear_run_directory(run_dir)
        if isinstance(exc, OSError):
            raise _local_spool_error(source_path, exc) from exc
        raise
    finally:
        fcntl.flock(registry_fd, fcntl.LOCK_UN)
        os.close(registry_fd)

    deadline = time.monotonic() + timeout_seconds
    while (return_code := process.poll()) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            termination_error = _terminate_without_wait(process)
            _TIMED_OUT_CHILDREN.append(process)
            os.close(key_fd)
            raise IsolatedSourceReadError(
                "SOURCE_READ_TIMEOUT",
                _evidence(
                    source_path,
                    "SOURCE_READ_TIMEOUT",
                    timeout_seconds=timeout_seconds,
                    termination_error_type=termination_error,
                ),
            )
        time.sleep(min(poll_interval_seconds, remaining))
    try:
        result = _load_local_result(run_dir, source_path=source_path, token=token)
        if return_code != 0:
            raise IsolatedSourceReadError(
                "SOURCE_READ_CHILD_FAILED",
                _evidence(source_path, "SOURCE_READ_CHILD_FAILED"),
            )
        return result
    finally:
        _cleanup_completed(root, key_path, run_dir, key_fd)


def _main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[1] != "--child":
        return 64
    return _child_copy(argv[2], Path(argv[3]), argv[4], int(argv[5]))


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
