"""Journaled recovery for an occupied canonical title/cover QC receipt.

The ordinary locator-successor writer is deliberately create-only.  This
module handles one narrower failure mode: the canonical candidate receipt name
already contains an older PASS bound to superseded cover bytes, while an
independent source authority can produce a zero-provider locator successor for
the current identical cover pixels.

The recovery is package-private and crash-resumable:

* the old canonical receipt is copied into an immutable operation archive;
* the exact projected successor bytes and a self-sealed plan are persisted;
* an append-only hash-chain journal records intent before any canonical swap;
* the old and new directory entries are exchanged atomically, never copied over
  an unknown concurrent file;
* native target replay, final source-authority replay and a second target replay
  must all pass before the transaction becomes VERIFIED;
* deterministic verification failure atomically restores the archived receipt;
* provider/image/media/upload calls remain zero and no publication authority is
  created.

This is not a generic way to rewrite failed model verdicts.  The source receipt
must already be a valid native PASS, source/destination cover and reference
bytes must match, and the only projected semantic change remains the existing
locator-successor envelope.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Callable, Iterator, Mapping

from src.autoslice.original_patch_package import sha_file
from src.autoslice.title_cover_qc_locator_successor import (
    _replay_source_authority_unchanged,
    _resolve_candidate_id,
    _resolve_package_inputs,
    _reuse_valid_qc,
    _root,
    _source_receipt_file,
    build_title_cover_qc_locator_successor,
)


PLAN_SCHEMA = "lidousha-title-cover-joint-qc-recovery-plan.v1"
JOURNAL_SCHEMA = "lidousha-title-cover-joint-qc-recovery-journal.v1"
RECEIPT_SCHEMA = "lidousha-title-cover-joint-qc-recovery-receipt.v1"
STATUS_VERIFIED = "VERIFIED_ZERO_PROVIDER_QC_RECOVERY"
_NAMESPACE = PurePosixPath("verification/title-cover-qc-recovery")
_MAX_JSON_BYTES = 2_000_000
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_STATES = frozenset(
    {
        "PLANNED",
        "INSTALL_INTENT",
        "INSTALLED",
        "VERIFIED",
        "ROLLBACK_INTENT",
        "ROLLED_BACK",
        "BLOCKED_DRIFT",
    }
)
_TRANSITIONS = {
    "PLANNED": frozenset({"INSTALL_INTENT", "BLOCKED_DRIFT"}),
    "INSTALL_INTENT": frozenset(
        {"INSTALLED", "ROLLBACK_INTENT", "BLOCKED_DRIFT"}
    ),
    "INSTALLED": frozenset(
        {"VERIFIED", "ROLLBACK_INTENT", "BLOCKED_DRIFT"}
    ),
    "ROLLBACK_INTENT": frozenset({"ROLLED_BACK", "BLOCKED_DRIFT"}),
    "VERIFIED": frozenset(),
    "ROLLED_BACK": frozenset(),
    "BLOCKED_DRIFT": frozenset(),
}


class TitleCoverQcRecoveryError(RuntimeError):
    """The occupied canonical receipt cannot be recovered safely."""


class _Binding(dict[str, object]):
    raw: bytes


class _Context:
    def __init__(
        self,
        *,
        destination_root: Path,
        candidate_id: str,
        title: str,
        current_cover: Path,
        current_cover_binding: Mapping[str, object],
        operation_id: str,
    ) -> None:
        self.destination_root = destination_root
        self.candidate_id = candidate_id
        self.title = title
        self.current_cover = current_cover
        self.current_cover_binding = dict(current_cover_binding)
        self.operation_id = operation_id
        self.canonical_path = (
            destination_root / f"{candidate_id}.title-cover-joint-qc.json"
        )
        self.verification_root = destination_root / "verification"
        self.namespace_root = destination_root / Path(_NAMESPACE)
        self.candidate_root = self.namespace_root / candidate_id
        self.operation_root = self.candidate_root / operation_id
        self.plan_path = self.operation_root / "plan.json"
        self.archive_path = self.operation_root / "superseded-receipt.json"
        self.successor_path = self.operation_root / "successor-receipt.json"
        self.journal_path = self.operation_root / "journal.jsonl"
        self.receipt_path = self.operation_root / "recovery-receipt.json"
        self.swap_path = (
            destination_root
            / f".{candidate_id}.title-cover-joint-qc.recovery-{operation_id}.swap"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TitleCoverQcRecoveryError(
            "TITLE_COVER_QC_RECOVERY_JSON_INVALID"
        ) from exc


def _pretty(value: Mapping[str, object], *, indent: int = 2) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                indent=indent,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TitleCoverQcRecoveryError(
            "TITLE_COVER_QC_RECOVERY_JSON_INVALID"
        ) from exc


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TitleCoverQcRecoveryError(f"{label} SHA-256 is missing")
    normalized = value.lower().removeprefix("sha256:")
    if not _SHA_RE.fullmatch(normalized):
        raise TitleCoverQcRecoveryError(f"{label} SHA-256 is invalid")
    return normalized


def _relative(path: Path, *, root: Path, label: str) -> str:
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise TitleCoverQcRecoveryError(f"{label} escapes its package") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise TitleCoverQcRecoveryError(f"{label} has an unsafe path")
    return relative.as_posix()


def _read_regular(
    path: Path,
    *,
    label: str,
    root: Path | None = None,
    mode: int | None = None,
    max_bytes: int = _MAX_JSON_BYTES,
) -> _Binding:
    absolute = path.absolute()
    if root is not None:
        _relative(absolute, root=root, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise TitleCoverQcRecoveryError(f"{label} is unavailable or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
            or not 0 < before.st_size <= max_bytes
        ):
            raise TitleCoverQcRecoveryError(f"{label} metadata is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise TitleCoverQcRecoveryError(f"{label} was truncated during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise TitleCoverQcRecoveryError(f"{label} grew during read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise TitleCoverQcRecoveryError(f"{label} changed during read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    binding = _Binding(
        path=str(absolute),
        sha256=_sha_bytes(raw),
        bytes=len(raw),
        device=before.st_dev,
        inode=before.st_ino,
        mode=stat.S_IMODE(before.st_mode),
    )
    binding.raw = raw
    return binding


def _read_json_binding(
    path: Path, *, label: str, root: Path, mode: int = 0o600
) -> tuple[_Binding, dict[str, Any]]:
    binding = _read_regular(path, label=label, root=root, mode=mode)
    try:
        value = json.loads(binding.raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TitleCoverQcRecoveryError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise TitleCoverQcRecoveryError(f"{label} is not a JSON object")
    return binding, value


def _file_binding(path: Path, *, root: Path, label: str) -> dict[str, object]:
    binding = _read_regular(
        path,
        label=label,
        root=root,
        mode=None,
        max_bytes=128_000_000,
    )
    return {
        "path": _relative(path, root=root, label=label),
        "sha256": binding["sha256"],
        "bytes": binding["bytes"],
    }


def _ensure_private_directory(path: Path, *, root: Path) -> Path:
    relative = Path(_relative(path, root=root, label="recovery directory"))
    cursor = root
    for component in relative.parts:
        cursor /= component
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            os.mkdir(cursor, mode=0o700)
            _fsync_directory(cursor.parent)
            metadata = os.lstat(cursor)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise TitleCoverQcRecoveryError(
                f"recovery directory is unsafe: {cursor}"
            )
    return cursor


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _package_lock(root: Path) -> Iterator[None]:
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise TitleCoverQcRecoveryError(
                "destination package root must be owner-private 0700"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_create_or_verify(path: Path, raw: bytes, *, root: Path) -> bool:
    _relative(path, root=root, label="recovery output")
    if path.exists() or path.is_symlink():
        existing = _read_regular(
            path, label="recovery output", root=root, mode=0o600
        )
        if existing.raw != raw:
            raise TitleCoverQcRecoveryError(
                f"existing recovery output differs: {path}"
            )
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_regular(
            path, label="recovery output", root=root, mode=0o600
        )
        if existing.raw != raw:
            raise TitleCoverQcRecoveryError(
                f"concurrent recovery output differs: {path}"
            )
        return False
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            wrote = os.write(descriptor, view)
            if wrote <= 0:
                raise OSError("short write")
            view = view[wrote:]
        os.fsync(descriptor)
    except BaseException:
        try:
            created = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = os.lstat(path)
        except OSError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (
                created.st_dev,
                created.st_ino,
            ):
                try:
                    os.unlink(path)
                    _fsync_directory(path.parent)
                except OSError:
                    pass
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return True


def _copy_create_or_verify(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    root: Path,
) -> bool:
    source_binding = _read_regular(
        source,
        label="recovery copy source",
        root=root,
        mode=0o600,
    )
    if source_binding["sha256"] != expected_sha256:
        raise TitleCoverQcRecoveryError(
            f"recovery copy source hash differs: {source}"
        )
    return _write_create_or_verify(
        destination, source_binding.raw, root=root
    )


def _unlink_owned(path: Path, *, expected_sha256: str, root: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    binding = _read_regular(
        path, label="recovery swap", root=root, mode=0o600
    )
    if binding["sha256"] != expected_sha256:
        raise TitleCoverQcRecoveryError(
            "recovery swap entry does not contain the expected owned bytes"
        )
    os.unlink(path)
    _fsync_directory(path.parent)


def _exchange_siblings(parent: Path, first: str, second: str) -> None:
    directory_fd = os.open(
        parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        first_raw = os.fsencode(first)
        second_raw = os.fsencode(second)
        if hasattr(libc, "renameat2"):
            operation = libc.renameat2
            operation.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            result = operation(
                directory_fd,
                first_raw,
                directory_fd,
                second_raw,
                2,  # Linux RENAME_EXCHANGE
            )
        elif hasattr(libc, "renameatx_np"):
            operation = libc.renameatx_np
            operation.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            result = operation(
                directory_fd,
                first_raw,
                directory_fd,
                second_raw,
                2,  # macOS RENAME_SWAP
            )
        else:
            raise TitleCoverQcRecoveryError(
                "atomic receipt exchange is unsupported on this host"
            )
        if result != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _operation_context(destination_package_root: Path, title: str) -> _Context:
    root = _root(destination_package_root, label="destination package root")
    record, _publish, cover = _resolve_package_inputs(root, title)
    candidate_id = _resolve_candidate_id(record, root)
    if not _CANDIDATE_RE.fullmatch(candidate_id):
        raise TitleCoverQcRecoveryError("candidate id is unsafe")
    cover_binding = _file_binding(
        cover, root=root, label="destination final cover"
    )
    seed = {
        "schema_version": PLAN_SCHEMA,
        "candidate_id": candidate_id,
        "title": title,
        "destination_package_root": str(root),
        "current_cover_sha256": cover_binding["sha256"],
    }
    operation_id = _sha_bytes(_canonical(seed))[:24]
    return _Context(
        destination_root=root,
        candidate_id=candidate_id,
        title=title,
        current_cover=cover,
        current_cover_binding=cover_binding,
        operation_id=operation_id,
    )


def _source_identity(
    source_package_root: Path, source_receipt_path: Path
) -> tuple[Path, Path, _Binding, str]:
    source_root = _root(source_package_root, label="source package root")
    safe_receipt = _source_receipt_file(source_root, source_receipt_path)
    binding = _read_regular(
        safe_receipt,
        label="source title/cover QC receipt",
        root=source_root,
        mode=0o600,
    )
    relative = _relative(
        safe_receipt, root=source_root, label="source title/cover QC receipt"
    )
    return source_root, safe_receipt, binding, relative


def _validate_stale_receipt(
    value: Mapping[str, object], *, candidate_id: str, title: str
) -> None:
    if not (
        value.get("schema_version") == "lidousha-title-cover-joint-qc.v1"
        and value.get("candidate_id") == candidate_id
        and value.get("title") == title
        and value.get("status") == "PASS"
        and value.get("pass") is True
    ):
        raise TitleCoverQcRecoveryError(
            "occupied canonical QC is not a same-candidate/title PASS receipt"
        )
    _normalize_sha(value.get("cover_sha256"), label="occupied QC cover")
    witness = value.get("witness")
    if not isinstance(witness, Mapping):
        raise TitleCoverQcRecoveryError("occupied canonical QC witness is missing")
    _normalize_sha(
        witness.get("image_sha256"), label="occupied QC witness image"
    )


def _projected_bytes(projected: Mapping[str, object]) -> bytes:
    return (
        json.dumps(projected, ensure_ascii=False, indent=1, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _plan_document(
    *,
    context: _Context,
    projected_at: str,
    projected_raw: bytes,
    old_binding: Mapping[str, object],
    source_root: Path,
    source_receipt: Path,
    source_binding: Mapping[str, object],
    source_relative: str,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": context.operation_id,
        "created_at": _utc_now(),
        "projected_at": projected_at,
        "candidate_id": context.candidate_id,
        "title": context.title,
        "title_sha256": _sha_bytes(context.title.encode("utf-8")),
        "destination_package_root": str(context.destination_root),
        "canonical_receipt_name": context.canonical_path.name,
        "current_cover": dict(context.current_cover_binding),
        "source_authority": {
            "package_root": str(source_root),
            "receipt_path": str(source_receipt),
            "receipt_relative_path": source_relative,
            "receipt_sha256": source_binding["sha256"],
            "receipt_bytes": source_binding["bytes"],
        },
        "superseded_receipt": {
            "canonical_path": context.canonical_path.name,
            "archive_path": _relative(
                context.archive_path,
                root=context.destination_root,
                label="superseded receipt archive",
            ),
            "sha256": old_binding["sha256"],
            "bytes": old_binding["bytes"],
            "device": old_binding["device"],
            "inode": old_binding["inode"],
        },
        "successor_receipt": {
            "evidence_path": _relative(
                context.successor_path,
                root=context.destination_root,
                label="successor receipt evidence",
            ),
            "canonical_path": context.canonical_path.name,
            "sha256": _sha_bytes(projected_raw),
            "bytes": len(projected_raw),
        },
        "journal_path": _relative(
            context.journal_path,
            root=context.destination_root,
            label="recovery journal",
        ),
        "recovery_receipt_path": _relative(
            context.receipt_path,
            root=context.destination_root,
            label="recovery receipt",
        ),
        "provider_calls": 0,
        "image_generation_calls": 0,
        "cover_mutations": 0,
        "upload_calls": 0,
        "upload_allowed": False,
    }
    plan["plan_sha256"] = _sha_bytes(_canonical(plan))
    return plan


def _load_plan(context: _Context) -> dict[str, Any]:
    _binding, plan = _read_json_binding(
        context.plan_path,
        label="title/cover QC recovery plan",
        root=context.destination_root,
    )
    supplied = plan.get("plan_sha256")
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    if not (
        plan.get("schema_version") == PLAN_SCHEMA
        and plan.get("plan_id") == context.operation_id
        and plan.get("candidate_id") == context.candidate_id
        and plan.get("title") == context.title
        and plan.get("destination_package_root")
        == str(context.destination_root)
        and plan.get("canonical_receipt_name") == context.canonical_path.name
        and plan.get("current_cover") == context.current_cover_binding
        and supplied == _sha_bytes(_canonical(unsigned))
    ):
        raise TitleCoverQcRecoveryError("title/cover QC recovery plan drifts")
    return plan


def _plan_sha(plan: Mapping[str, object]) -> str:
    value = plan.get("plan_sha256")
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise TitleCoverQcRecoveryError("recovery plan digest is invalid")
    return value


def _read_journal(context: _Context, plan: Mapping[str, object]) -> list[dict[str, Any]]:
    path = context.journal_path
    if not path.exists() and not path.is_symlink():
        return []
    binding = _read_regular(
        path,
        label="title/cover QC recovery journal",
        root=context.destination_root,
        mode=0o600,
    )
    raw = binding.raw
    if raw and not raw.endswith(b"\n"):
        raise TitleCoverQcRecoveryError("recovery journal has a partial final row")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise TitleCoverQcRecoveryError("recovery journal is not UTF-8") from exc
    rows: list[dict[str, Any]] = []
    previous_hash: str | None = None
    previous_state: str | None = None
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise TitleCoverQcRecoveryError(
                f"recovery journal row {line_number} is empty"
            )
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TitleCoverQcRecoveryError(
                f"recovery journal row {line_number} is invalid"
            ) from exc
        if not isinstance(row, dict):
            raise TitleCoverQcRecoveryError(
                f"recovery journal row {line_number} is not an object"
            )
        supplied = row.get("row_sha256")
        unsigned = dict(row)
        unsigned.pop("row_sha256", None)
        state = row.get("state")
        if not (
            row.get("schema_version") == JOURNAL_SCHEMA
            and row.get("seq") == line_number
            and row.get("previous_row_sha256") == previous_hash
            and row.get("plan_id") == plan.get("plan_id")
            and row.get("plan_sha256") == _plan_sha(plan)
            and state in _STATES
            and supplied == _sha_bytes(_canonical(unsigned))
        ):
            raise TitleCoverQcRecoveryError(
                f"recovery journal row {line_number} binding drifts"
            )
        if previous_state is None:
            if state != "PLANNED":
                raise TitleCoverQcRecoveryError(
                    "recovery journal does not begin at PLANNED"
                )
        elif state not in _TRANSITIONS[previous_state]:
            raise TitleCoverQcRecoveryError(
                f"illegal recovery transition {previous_state}->{state}"
            )
        previous_hash = str(supplied)
        previous_state = str(state)
        rows.append(row)
    return rows


def _append_journal(
    context: _Context,
    plan: Mapping[str, object],
    *,
    state: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    rows = _read_journal(context, plan)
    if rows:
        prior = str(rows[-1]["state"])
        if state not in _TRANSITIONS[prior]:
            raise TitleCoverQcRecoveryError(
                f"illegal recovery transition {prior}->{state}"
            )
    elif state != "PLANNED":
        raise TitleCoverQcRecoveryError("recovery journal must begin PLANNED")
    row: dict[str, Any] = {
        "schema_version": JOURNAL_SCHEMA,
        "seq": len(rows) + 1,
        "previous_row_sha256": rows[-1]["row_sha256"] if rows else None,
        "at": _utc_now(),
        "plan_id": plan["plan_id"],
        "plan_sha256": _plan_sha(plan),
        "state": state,
        "details": dict(details or {}),
    }
    row["row_sha256"] = _sha_bytes(_canonical(row))
    raw = _canonical(row) + b"\n"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(context.journal_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TitleCoverQcRecoveryError("recovery journal mode is unsafe")
        view = memoryview(raw)
        while view:
            wrote = os.write(descriptor, view)
            if wrote <= 0:
                raise OSError("short journal write")
            view = view[wrote:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(context.journal_path.parent)
    return row


def _binding_from_plan(plan: Mapping[str, object], section: str) -> Mapping[str, object]:
    value = plan.get(section)
    if not isinstance(value, Mapping):
        raise TitleCoverQcRecoveryError(f"recovery plan {section} is invalid")
    return value


def _canonical_state(context: _Context, plan: Mapping[str, object]) -> str:
    binding = _read_regular(
        context.canonical_path,
        label="canonical title/cover QC receipt",
        root=context.destination_root,
        mode=0o600,
    )
    old = _normalize_sha(
        _binding_from_plan(plan, "superseded_receipt").get("sha256"),
        label="superseded receipt",
    )
    new = _normalize_sha(
        _binding_from_plan(plan, "successor_receipt").get("sha256"),
        label="successor receipt",
    )
    digest = str(binding["sha256"])
    if digest == old:
        return "SUPERSEDED"
    if digest == new:
        return "SUCCESSOR"
    return "OTHER"


def _project_from_plan(
    context: _Context,
    plan: Mapping[str, object],
    *,
    source_package_root: Path,
    source_receipt_path: Path,
) -> tuple[dict[str, Any], bytes]:
    source = _binding_from_plan(plan, "source_authority")
    expected_source_root = Path(str(source.get("package_root") or "")).absolute()
    supplied_source_root = Path(source_package_root).absolute()
    if supplied_source_root != expected_source_root:
        raise TitleCoverQcRecoveryError("source package differs from recovery plan")
    expected_receipt = Path(str(source.get("receipt_path") or "")).absolute()
    if Path(source_receipt_path).absolute() != expected_receipt:
        raise TitleCoverQcRecoveryError("source receipt differs from recovery plan")
    safe_root, safe_receipt, source_binding, relative = _source_identity(
        supplied_source_root, source_receipt_path
    )
    if not (
        str(safe_root) == str(expected_source_root)
        and str(safe_receipt) == str(expected_receipt)
        and relative == source.get("receipt_relative_path")
        and source_binding["sha256"] == source.get("receipt_sha256")
        and source_binding["bytes"] == source.get("receipt_bytes")
    ):
        raise TitleCoverQcRecoveryError("source authority receipt drifts")
    projected_at = plan.get("projected_at")
    if not isinstance(projected_at, str) or not projected_at:
        raise TitleCoverQcRecoveryError("recovery projection timestamp is invalid")
    projected = build_title_cover_qc_locator_successor(
        source_package_root=safe_root,
        source_receipt_path=safe_receipt,
        destination_package_root=context.destination_root,
        title=context.title,
        projected_at=projected_at,
    )
    projected_raw = _projected_bytes(projected)
    successor = _binding_from_plan(plan, "successor_receipt")
    if not (
        _sha_bytes(projected_raw)
        == _normalize_sha(successor.get("sha256"), label="successor receipt")
        and len(projected_raw) == successor.get("bytes")
    ):
        raise TitleCoverQcRecoveryError("replayed successor projection drifts")
    return projected, projected_raw


def _validate_plan_evidence(
    context: _Context,
    plan: Mapping[str, object],
    *,
    source_package_root: Path,
    source_receipt_path: Path,
) -> dict[str, Any]:
    projected, projected_raw = _project_from_plan(
        context,
        plan,
        source_package_root=source_package_root,
        source_receipt_path=source_receipt_path,
    )
    archive = _read_regular(
        context.archive_path,
        label="superseded receipt archive",
        root=context.destination_root,
        mode=0o600,
    )
    old = _binding_from_plan(plan, "superseded_receipt")
    if not (
        archive["sha256"]
        == _normalize_sha(old.get("sha256"), label="superseded receipt")
        and archive["bytes"] == old.get("bytes")
    ):
        raise TitleCoverQcRecoveryError("superseded receipt archive drifts")
    successor_evidence = _read_regular(
        context.successor_path,
        label="successor receipt evidence",
        root=context.destination_root,
        mode=0o600,
    )
    if successor_evidence.raw != projected_raw:
        raise TitleCoverQcRecoveryError("successor receipt evidence drifts")
    return projected


def _prepare_initial_plan(
    context: _Context,
    *,
    source_package_root: Path,
    source_receipt_path: Path,
    checkpoint: Callable[[str], None] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    old_binding, old_value = _read_json_binding(
        context.canonical_path,
        label="occupied canonical title/cover QC receipt",
        root=context.destination_root,
    )
    try:
        _reuse_valid_qc(
            context.destination_root, context.title, context.canonical_path
        )
    except (OSError, ValueError, RuntimeError):
        pass
    else:
        return {"status": "ALREADY_VALID"}, old_value
    _validate_stale_receipt(
        old_value, candidate_id=context.candidate_id, title=context.title
    )
    source_root, source_receipt, source_binding, source_relative = _source_identity(
        source_package_root, source_receipt_path
    )
    projected_at = _utc_now()
    projected = build_title_cover_qc_locator_successor(
        source_package_root=source_root,
        source_receipt_path=source_receipt,
        destination_package_root=context.destination_root,
        title=context.title,
        projected_at=projected_at,
    )
    projected_raw = _projected_bytes(projected)
    plan = _plan_document(
        context=context,
        projected_at=projected_at,
        projected_raw=projected_raw,
        old_binding=old_binding,
        source_root=source_root,
        source_receipt=source_receipt,
        source_binding=source_binding,
        source_relative=source_relative,
    )
    _ensure_private_directory(
        context.operation_root, root=context.destination_root
    )
    _write_create_or_verify(
        context.plan_path, _pretty(plan), root=context.destination_root
    )
    _checkpoint(checkpoint, "after_plan_persisted")
    _write_create_or_verify(
        context.archive_path,
        old_binding.raw,
        root=context.destination_root,
    )
    _write_create_or_verify(
        context.successor_path, projected_raw, root=context.destination_root
    )
    _append_journal(
        context,
        plan,
        state="PLANNED",
        details={
            "remote_mutation": False,
            "provider_calls": 0,
            "superseded_receipt_sha256": old_binding["sha256"],
            "successor_receipt_sha256": _sha_bytes(projected_raw),
        },
    )
    return plan, projected


def _complete_unjournaled_plan(
    context: _Context,
    plan: Mapping[str, object],
    *,
    source_package_root: Path,
    source_receipt_path: Path,
) -> dict[str, Any]:
    if _canonical_state(context, plan) != "SUPERSEDED":
        raise TitleCoverQcRecoveryError(
            "unjournaled recovery plan no longer owns the canonical receipt"
        )
    canonical = _read_regular(
        context.canonical_path,
        label="unjournaled superseded canonical receipt",
        root=context.destination_root,
        mode=0o600,
    )
    old = _binding_from_plan(plan, "superseded_receipt")
    old_sha = _normalize_sha(old.get("sha256"), label="superseded receipt")
    if not (
        canonical["sha256"] == old_sha
        and canonical["bytes"] == old.get("bytes")
    ):
        raise TitleCoverQcRecoveryError(
            "unjournaled canonical receipt differs from the recovery plan"
        )
    projected, projected_raw = _project_from_plan(
        context,
        plan,
        source_package_root=source_package_root,
        source_receipt_path=source_receipt_path,
    )
    _write_create_or_verify(
        context.archive_path, canonical.raw, root=context.destination_root
    )
    _write_create_or_verify(
        context.successor_path, projected_raw, root=context.destination_root
    )
    _append_journal(
        context,
        plan,
        state="PLANNED",
        details={
            "resumed_before_first_journal_row": True,
            "remote_mutation": False,
            "provider_calls": 0,
            "superseded_receipt_sha256": old_sha,
            "successor_receipt_sha256": _sha_bytes(projected_raw),
        },
    )
    return projected


def _ensure_swap(
    context: _Context, *, source: Path, expected_sha256: str
) -> None:
    _copy_create_or_verify(
        source,
        context.swap_path,
        expected_sha256=expected_sha256,
        root=context.destination_root,
    )


def _checkpoint(callback: Callable[[str], None] | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _append_blocked(
    context: _Context,
    plan: Mapping[str, object],
    *,
    detail: str,
) -> None:
    rows = _read_journal(context, plan)
    if rows and "BLOCKED_DRIFT" in _TRANSITIONS[str(rows[-1]["state"])]:
        _append_journal(
            context,
            plan,
            state="BLOCKED_DRIFT",
            details={"reason": detail, "remote_mutation": False},
        )


def _reject_exchange_mismatch(
    context: _Context,
    plan: Mapping[str, object],
    *,
    canonical_sha256: str,
    swap_sha256: str,
    old_sha256: str,
    new_sha256: str,
) -> None:
    detail = (
        "atomic exchange ownership mismatch: "
        f"canonical={canonical_sha256} swap={swap_sha256}"
    )
    if canonical_sha256 == new_sha256 and swap_sha256 != old_sha256:
        # A third party replaced the canonical name after our pre-exchange
        # ownership check. The exchange moved those bytes into the swap name;
        # exchange back immediately, then remove only our known successor.
        _exchange_siblings(
            context.destination_root,
            context.canonical_path.name,
            context.swap_path.name,
        )
        restored = _read_regular(
            context.canonical_path,
            label="restored third-party canonical QC",
            root=context.destination_root,
            mode=0o600,
        )
        displaced = _read_regular(
            context.swap_path,
            label="displaced owned successor QC",
            root=context.destination_root,
            mode=0o600,
        )
        if (
            restored["sha256"] != swap_sha256
            or displaced["sha256"] != new_sha256
        ):
            raise TitleCoverQcRecoveryError(
                "atomic exchange mismatch could not restore third-party bytes"
            )
        _unlink_owned(
            context.swap_path,
            expected_sha256=new_sha256,
            root=context.destination_root,
        )
    elif canonical_sha256 not in {old_sha256, new_sha256}:
        # Preserve the unknown canonical entry in place. The archive already
        # preserves the superseded receipt, so remove only a known operation
        # entry from the swap name.
        if swap_sha256 in {old_sha256, new_sha256}:
            _unlink_owned(
                context.swap_path,
                expected_sha256=swap_sha256,
                root=context.destination_root,
            )
    elif canonical_sha256 == old_sha256:
        # No successor was installed. Keep the old canonical and remove only a
        # known operation-owned swap entry.
        if swap_sha256 in {old_sha256, new_sha256}:
            _unlink_owned(
                context.swap_path,
                expected_sha256=swap_sha256,
                root=context.destination_root,
            )
    _append_blocked(context, plan, detail=detail)
    raise TitleCoverQcRecoveryError(
        "atomic title/cover QC exchange encountered unowned bytes"
    )


def _install_successor(
    context: _Context,
    plan: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None] | None,
) -> None:
    old_sha = _normalize_sha(
        _binding_from_plan(plan, "superseded_receipt").get("sha256"),
        label="superseded receipt",
    )
    new_sha = _normalize_sha(
        _binding_from_plan(plan, "successor_receipt").get("sha256"),
        label="successor receipt",
    )
    state = _canonical_state(context, plan)
    rows = _read_journal(context, plan)
    last = str(rows[-1]["state"])
    if state == "SUCCESSOR":
        if context.swap_path.exists() or context.swap_path.is_symlink():
            swap = _read_regular(
                context.swap_path,
                label="recovery swap",
                root=context.destination_root,
                mode=0o600,
            )
            if swap["sha256"] != old_sha:
                _reject_exchange_mismatch(
                    context,
                    plan,
                    canonical_sha256=new_sha,
                    swap_sha256=str(swap["sha256"]),
                    old_sha256=old_sha,
                    new_sha256=new_sha,
                )
        if last == "INSTALL_INTENT":
            _append_journal(
                context,
                plan,
                state="INSTALLED",
                details={
                    "canonical_sha256": new_sha,
                    "superseded_swap_sha256": old_sha,
                    "resumed_after_exchange": True,
                },
            )
        elif last not in {"INSTALLED"}:
            raise TitleCoverQcRecoveryError(
                f"successor canonical is inconsistent with journal state {last}"
            )
        return
    if state != "SUPERSEDED":
        _append_blocked(
            context, plan, detail="canonical receipt has a third-party digest"
        )
        raise TitleCoverQcRecoveryError(
            "canonical title/cover QC receipt has unowned bytes"
        )
    if last == "PLANNED":
        _ensure_swap(context, source=context.successor_path, expected_sha256=new_sha)
        _append_journal(
            context,
            plan,
            state="INSTALL_INTENT",
            details={
                "canonical_before_sha256": old_sha,
                "swap_before_sha256": new_sha,
                "atomic_exchange": True,
                "remote_mutation": False,
            },
        )
        _checkpoint(checkpoint, "after_install_intent")
    elif last != "INSTALL_INTENT":
        raise TitleCoverQcRecoveryError(
            f"superseded canonical is inconsistent with journal state {last}"
        )
    _ensure_swap(context, source=context.successor_path, expected_sha256=new_sha)
    if _canonical_state(context, plan) != "SUPERSEDED":
        _append_blocked(
            context, plan, detail="canonical receipt changed before exchange"
        )
        raise TitleCoverQcRecoveryError(
            "canonical title/cover QC changed before atomic exchange"
        )
    _exchange_siblings(
        context.destination_root,
        context.canonical_path.name,
        context.swap_path.name,
    )
    _checkpoint(checkpoint, "after_exchange")
    canonical = _read_regular(
        context.canonical_path,
        label="installed canonical QC",
        root=context.destination_root,
        mode=0o600,
    )
    swap = _read_regular(
        context.swap_path,
        label="superseded QC swap",
        root=context.destination_root,
        mode=0o600,
    )
    if canonical["sha256"] != new_sha or swap["sha256"] != old_sha:
        _reject_exchange_mismatch(
            context,
            plan,
            canonical_sha256=str(canonical["sha256"]),
            swap_sha256=str(swap["sha256"]),
            old_sha256=old_sha,
            new_sha256=new_sha,
        )
    _append_journal(
        context,
        plan,
        state="INSTALLED",
        details={
            "canonical_sha256": new_sha,
            "superseded_swap_sha256": old_sha,
            "resumed_after_exchange": False,
        },
    )
    _checkpoint(checkpoint, "after_installed")


def _restore_superseded(
    context: _Context,
    plan: Mapping[str, object],
    *,
    checkpoint: Callable[[str], None] | None,
) -> None:
    old_sha = _normalize_sha(
        _binding_from_plan(plan, "superseded_receipt").get("sha256"),
        label="superseded receipt",
    )
    new_sha = _normalize_sha(
        _binding_from_plan(plan, "successor_receipt").get("sha256"),
        label="successor receipt",
    )
    state = _canonical_state(context, plan)
    if state == "SUPERSEDED":
        if context.swap_path.exists() or context.swap_path.is_symlink():
            _unlink_owned(
                context.swap_path,
                expected_sha256=new_sha,
                root=context.destination_root,
            )
        return
    if state != "SUCCESSOR":
        _append_blocked(
            context, plan, detail="cannot rollback an unowned canonical digest"
        )
        raise TitleCoverQcRecoveryError(
            "canonical title/cover QC drift prevents rollback"
        )
    if context.swap_path.exists() or context.swap_path.is_symlink():
        swap = _read_regular(
            context.swap_path,
            label="rollback swap",
            root=context.destination_root,
            mode=0o600,
        )
        if swap["sha256"] != old_sha:
            raise TitleCoverQcRecoveryError(
                "rollback swap does not contain the superseded receipt"
            )
    else:
        _ensure_swap(context, source=context.archive_path, expected_sha256=old_sha)
    _exchange_siblings(
        context.destination_root,
        context.canonical_path.name,
        context.swap_path.name,
    )
    _checkpoint(checkpoint, "after_rollback_exchange")
    if _canonical_state(context, plan) != "SUPERSEDED":
        raise TitleCoverQcRecoveryError(
            "rollback failed to restore the superseded canonical receipt"
        )
    _unlink_owned(
        context.swap_path,
        expected_sha256=new_sha,
        root=context.destination_root,
    )


def _verify_installed(
    context: _Context,
    plan: Mapping[str, object],
    projected: Mapping[str, object],
    *,
    source_package_root: Path,
    source_receipt_path: Path,
) -> dict[str, Any]:
    successor = _binding_from_plan(plan, "successor_receipt")
    expected_sha = _normalize_sha(
        successor.get("sha256"), label="successor receipt"
    )
    canonical = _read_regular(
        context.canonical_path,
        label="installed canonical title/cover QC receipt",
        root=context.destination_root,
        mode=0o600,
    )
    if canonical["sha256"] != expected_sha:
        raise TitleCoverQcRecoveryError("installed canonical QC hash drifts")
    first = _reuse_valid_qc(
        context.destination_root, context.title, context.canonical_path
    )
    if first != dict(projected):
        raise TitleCoverQcRecoveryError(
            "installed target replay differs from projected successor"
        )
    source = projected.get("locator_successor")
    if not isinstance(source, Mapping):
        raise TitleCoverQcRecoveryError(
            "projected successor lacks source authority binding"
        )
    _replay_source_authority_unchanged(
        source_package_root=source_package_root,
        source_receipt_path=source_receipt_path,
        title=context.title,
        expected_sha256=_normalize_sha(
            source.get("source_receipt_sha256"), label="source authority"
        ),
    )
    second = _reuse_valid_qc(
        context.destination_root, context.title, context.canonical_path
    )
    if second != dict(projected):
        raise TitleCoverQcRecoveryError(
            "destination package changed during final source replay"
        )
    if sha_file(context.canonical_path) != expected_sha:
        raise TitleCoverQcRecoveryError(
            "canonical QC changed after final target replay"
        )
    return second


def _receipt_document(
    context: _Context,
    plan: Mapping[str, object],
    terminal: Mapping[str, object],
) -> dict[str, object]:
    canonical = _read_regular(
        context.canonical_path,
        label="verified canonical QC receipt",
        root=context.destination_root,
        mode=0o600,
    )
    archive = _read_regular(
        context.archive_path,
        label="superseded QC archive",
        root=context.destination_root,
        mode=0o600,
    )
    plan_binding = _read_regular(
        context.plan_path,
        label="QC recovery plan",
        root=context.destination_root,
        mode=0o600,
    )
    journal_binding = _read_regular(
        context.journal_path,
        label="QC recovery journal",
        root=context.destination_root,
        mode=0o600,
    )
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "status": STATUS_VERIFIED,
        "candidate_id": context.candidate_id,
        "title": context.title,
        "destination_package_root": str(context.destination_root),
        "plan": {
            "path": _relative(
                context.plan_path,
                root=context.destination_root,
                label="QC recovery plan",
            ),
            "sha256": plan_binding["sha256"],
            "bytes": plan_binding["bytes"],
            "plan_sha256": _plan_sha(plan),
        },
        "journal": {
            "path": _relative(
                context.journal_path,
                root=context.destination_root,
                label="QC recovery journal",
            ),
            "sha256": journal_binding["sha256"],
            "bytes": journal_binding["bytes"],
            "verified_row_sha256": terminal["row_sha256"],
        },
        "superseded_receipt": {
            "path": _relative(
                context.archive_path,
                root=context.destination_root,
                label="superseded QC archive",
            ),
            "sha256": archive["sha256"],
            "bytes": archive["bytes"],
        },
        "canonical_receipt": {
            "path": context.canonical_path.name,
            "sha256": canonical["sha256"],
            "bytes": canonical["bytes"],
        },
        "source_authority": dict(
            _binding_from_plan(plan, "source_authority")
        ),
        "current_cover": dict(context.current_cover_binding),
        "provider_calls": 0,
        "image_generation_calls": 0,
        "cover_mutations": 0,
        "title_mutations": 0,
        "media_mutations": 0,
        "upload_calls": 0,
        "upload_allowed": False,
    }
    receipt["receipt_sha256"] = _sha_bytes(_canonical(receipt))
    return receipt


def _persist_verified_receipt(
    context: _Context,
    plan: Mapping[str, object],
    terminal: Mapping[str, object],
) -> dict[str, object]:
    receipt = _receipt_document(context, plan, terminal)
    _write_create_or_verify(
        context.receipt_path, _pretty(receipt), root=context.destination_root
    )
    _binding, stored = _read_json_binding(
        context.receipt_path,
        label="title/cover QC recovery receipt",
        root=context.destination_root,
    )
    if stored != receipt:
        raise TitleCoverQcRecoveryError("stored QC recovery receipt drifts")
    return receipt


def plan_title_cover_qc_receipt_recovery(
    *,
    source_package_root: Path,
    source_receipt_path: Path,
    destination_package_root: Path,
    title: str,
) -> dict[str, object]:
    """Validate a possible recovery without creating package bytes."""

    context = _operation_context(destination_package_root, title)
    with _package_lock(context.destination_root):
        try:
            current = _reuse_valid_qc(
                context.destination_root, context.title, context.canonical_path
            )
        except (OSError, ValueError, RuntimeError):
            current = None
        if current is not None:
            return {
                "status": "ALREADY_VALID",
                "candidate_id": context.candidate_id,
                "canonical_receipt": str(context.canonical_path),
                "provider_calls": 0,
                "writes": 0,
            }
        old_binding, old_value = _read_json_binding(
            context.canonical_path,
            label="occupied canonical title/cover QC receipt",
            root=context.destination_root,
        )
        _validate_stale_receipt(
            old_value, candidate_id=context.candidate_id, title=context.title
        )
        source_root, source_receipt, source_binding, _relative_source = (
            _source_identity(source_package_root, source_receipt_path)
        )
        projected_at = _utc_now()
        projected = build_title_cover_qc_locator_successor(
            source_package_root=source_root,
            source_receipt_path=source_receipt,
            destination_package_root=context.destination_root,
            title=context.title,
            projected_at=projected_at,
        )
        projected_raw = _projected_bytes(projected)
        return {
            "status": "WOULD_RECOVER",
            "candidate_id": context.candidate_id,
            "title": context.title,
            "canonical_receipt": str(context.canonical_path),
            "superseded_receipt_sha256": old_binding["sha256"],
            "source_receipt_sha256": source_binding["sha256"],
            "successor_receipt_sha256": _sha_bytes(projected_raw),
            "current_cover_sha256": context.current_cover_binding["sha256"],
            "operation_id": context.operation_id,
            "provider_calls": 0,
            "writes": 0,
        }


def recover_title_cover_qc_receipt(
    *,
    source_package_root: Path,
    source_receipt_path: Path,
    destination_package_root: Path,
    title: str,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Recover the occupied canonical QC receipt through a durable transaction."""

    context = _operation_context(destination_package_root, title)
    with _package_lock(context.destination_root):
        created_plan = False
        if context.plan_path.exists() or context.plan_path.is_symlink():
            plan = _load_plan(context)
        else:
            prepared, projected = _prepare_initial_plan(
                context,
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
                checkpoint=checkpoint,
            )
            if prepared.get("status") == "ALREADY_VALID":
                return {
                    "status": "ALREADY_VALID",
                    "candidate_id": context.candidate_id,
                    "canonical_receipt": str(context.canonical_path),
                    "provider_calls": 0,
                    "cache_reused": True,
                }
            plan = prepared
            created_plan = True
            _checkpoint(checkpoint, "after_planned")
        rows = _read_journal(context, plan)
        if not rows:
            _complete_unjournaled_plan(
                context,
                plan,
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
            )
            _checkpoint(checkpoint, "after_planned")
            rows = _read_journal(context, plan)
        last_state = str(rows[-1]["state"])
        if last_state == "VERIFIED":
            projected = _validate_plan_evidence(
                context,
                plan,
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
            )
            _verify_installed(
                context,
                plan,
                projected,
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
            )
            old_sha = _normalize_sha(
                _binding_from_plan(plan, "superseded_receipt").get("sha256"),
                label="superseded receipt",
            )
            if context.swap_path.exists() or context.swap_path.is_symlink():
                _unlink_owned(
                    context.swap_path,
                    expected_sha256=old_sha,
                    root=context.destination_root,
                )
            receipt = _persist_verified_receipt(context, plan, rows[-1])
            return {
                "status": STATUS_VERIFIED,
                "candidate_id": context.candidate_id,
                "canonical_receipt": str(context.canonical_path),
                "recovery_receipt": str(context.receipt_path),
                "journal": str(context.journal_path),
                "archive": str(context.archive_path),
                "terminal_row_sha256": rows[-1]["row_sha256"],
                "provider_calls": 0,
                "cache_reused": True,
                "receipt": receipt,
            }
        if last_state in {"ROLLED_BACK", "BLOCKED_DRIFT"}:
            raise TitleCoverQcRecoveryError(
                f"recovery operation is terminal {last_state}"
            )
        if last_state == "ROLLBACK_INTENT":
            _restore_superseded(context, plan, checkpoint=checkpoint)
            terminal = _append_journal(
                context,
                plan,
                state="ROLLED_BACK",
                details={"resumed": True, "remote_mutation": False},
            )
            raise TitleCoverQcRecoveryError(
                "prior verification failure was rolled back: "
                + str(terminal["row_sha256"])
            )
        try:
            projected = _validate_plan_evidence(
                context,
                plan,
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
            )
        except Exception as exc:
            if _canonical_state(context, plan) == "SUCCESSOR":
                current_rows = _read_journal(context, plan)
                current_state = str(current_rows[-1]["state"])
                if "ROLLBACK_INTENT" in _TRANSITIONS[current_state]:
                    _append_journal(
                        context,
                        plan,
                        state="ROLLBACK_INTENT",
                        details={
                            "reason": type(exc).__name__,
                            "detail": str(exc)[:512],
                            "remote_mutation": False,
                        },
                    )
                _restore_superseded(context, plan, checkpoint=checkpoint)
                _append_journal(
                    context,
                    plan,
                    state="ROLLED_BACK",
                    details={
                        "reason": "plan evidence drift after install",
                        "remote_mutation": False,
                    },
                )
            else:
                _append_blocked(
                    context, plan, detail=f"plan evidence drift: {type(exc).__name__}"
                )
            raise TitleCoverQcRecoveryError(
                f"recovery plan evidence is no longer valid: {exc}"
            ) from exc
        _install_successor(context, plan, checkpoint=checkpoint)
        try:
            replayed = _verify_installed(
                context,
                plan,
                projected,
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
            )
        except Exception as exc:
            _append_journal(
                context,
                plan,
                state="ROLLBACK_INTENT",
                details={
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:512],
                    "remote_mutation": False,
                },
            )
            _checkpoint(checkpoint, "after_rollback_intent")
            try:
                _restore_superseded(context, plan, checkpoint=checkpoint)
            except Exception as rollback_exc:
                _append_blocked(
                    context,
                    plan,
                    detail=f"rollback failed: {type(rollback_exc).__name__}",
                )
                raise TitleCoverQcRecoveryError(
                    f"target replay failed and rollback could not close: {rollback_exc}"
                ) from rollback_exc
            _append_journal(
                context,
                plan,
                state="ROLLED_BACK",
                details={
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:512],
                    "remote_mutation": False,
                },
            )
            raise TitleCoverQcRecoveryError(
                f"target replay failed; superseded receipt restored: {exc}"
            ) from exc
        old_sha = _normalize_sha(
            _binding_from_plan(plan, "superseded_receipt").get("sha256"),
            label="superseded receipt",
        )
        if context.swap_path.exists() or context.swap_path.is_symlink():
            _unlink_owned(
                context.swap_path,
                expected_sha256=old_sha,
                root=context.destination_root,
            )
        terminal = _append_journal(
            context,
            plan,
            state="VERIFIED",
            details={
                "canonical_receipt_sha256": sha_file(context.canonical_path),
                "source_authority_sha256": _binding_from_plan(
                    plan, "source_authority"
                ).get("receipt_sha256"),
                "provider_calls": 0,
                "remote_mutation": False,
            },
        )
        _checkpoint(checkpoint, "after_verified")
        receipt = _persist_verified_receipt(context, plan, terminal)
        return {
            "status": STATUS_VERIFIED,
            "candidate_id": context.candidate_id,
            "canonical_receipt": str(context.canonical_path),
            "recovery_receipt": str(context.receipt_path),
            "journal": str(context.journal_path),
            "archive": str(context.archive_path),
            "terminal_row_sha256": terminal["row_sha256"],
            "provider_calls": 0,
            "cache_reused": not created_plan,
            "receipt": receipt,
            "qc": replayed,
        }


__all__ = [
    "JOURNAL_SCHEMA",
    "PLAN_SCHEMA",
    "RECEIPT_SCHEMA",
    "STATUS_VERIFIED",
    "TitleCoverQcRecoveryError",
    "plan_title_cover_qc_receipt_recovery",
    "recover_title_cover_qc_receipt",
]
