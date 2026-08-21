"""Observational diagnostics for the sealed Qixi public-surface closure.

This module deliberately has no transaction, provider, or runtime mutation
entry point.  It records independently evaluable predicates for an operator
to inspect while leaving the finalizer's fail-closed formal validators as the
only authority that can approve an after-image.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PredicateStatus(StrEnum):
    """The observation state of one public-surface predicate."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"
    NEEDS_PROVIDER = "NEEDS_PROVIDER"


@dataclass(frozen=True, slots=True)
class PredicateResult:
    """A deliberately sanitized, non-authoritative predicate observation."""

    name: str
    status: PredicateStatus
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status.value, "reason": self.reason}


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def collect_predicates(
    checks: Sequence[tuple[str, Callable[[], bool]]],
    *,
    expected_errors: tuple[type[Exception], ...] = (),
) -> list[PredicateResult]:
    """Evaluate every independent check, retaining no exception payloads.

    The callback contract is intentionally boolean.  A caller can continue
    using its precise formal validator for admission while this collector
    avoids leaking paths, prompts, HTTP responses, tokens, or media bytes.
    """

    results: list[PredicateResult] = []
    for name, check in checks:
        try:
            passed = check()
        except expected_errors:
            results.append(
                PredicateResult(
                    name=name,
                    status=PredicateStatus.FAIL,
                    reason="PREDICATE_REJECTED",
                )
            )
            continue
        except Exception:
            results.append(
                PredicateResult(
                    name=name,
                    status=PredicateStatus.FAIL,
                    reason="UNEXPECTED_EXCEPTION",
                )
            )
            continue
        results.append(
            PredicateResult(
                name=name,
                status=PredicateStatus.PASS if passed else PredicateStatus.FAIL,
                reason="SATISFIED" if passed else "PREDICATE_REJECTED",
            )
        )
    return results


def unavailable_predicates(
    names: Sequence[str], *, needs_provider: bool = False
) -> list[PredicateResult]:
    status = PredicateStatus.NEEDS_PROVIDER if needs_provider else PredicateStatus.NOT_EVALUATED
    reason = "PROVIDER_REQUIRED" if needs_provider else "DEPENDENT_INPUT_UNAVAILABLE"
    return [PredicateResult(name=name, status=status, reason=reason) for name in names]


def matrix_document(results: Sequence[PredicateResult]) -> dict[str, object]:
    names = [result.name for result in results]
    if len(names) != len(set(names)):
        raise ValueError("diagnostic predicate names must be unique")
    return {
        "schema_version": "qixi-public-surface-predicate-matrix.v1",
        "observational_only": True,
        "predicates": [result.as_dict() for result in results],
    }


def _require_safe_directory(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = os.lstat(cursor)
        except OSError as exc:
            raise RuntimeError("diagnostic receipt directory is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("diagnostic receipt directory is unsafe")
        if cursor == path and (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()):
            raise RuntimeError("diagnostic receipt directory ownership drifts")


def diagnostic_root(*, candidate_root: Path, authority_sha256: str) -> Path:
    """Return the fixed create-only diagnostic namespace for one authority."""

    if not authority_sha256.startswith("sha256:") or len(authority_sha256) != 71:
        raise RuntimeError("diagnostic authority hash is invalid")
    try:
        candidate_root = candidate_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("diagnostic candidate root is unavailable") from exc
    _require_safe_directory(candidate_root)
    parent = candidate_root / "qixi_post_correction_diagnostics"
    leaf = parent / authority_sha256[7:23]
    for directory in (parent, leaf):
        if os.path.lexists(directory):
            _require_safe_directory(directory)
            if stat.S_IMODE(os.lstat(directory).st_mode) != 0o700:
                raise RuntimeError("diagnostic receipt directory mode drifts")
            continue
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            _require_safe_directory(directory)
        else:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fchmod(fd, 0o700)
                os.fsync(fd)
            finally:
                os.close(fd)
            fd = os.open(directory.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _require_safe_directory(directory)
    return leaf


def write_failure_receipt(
    *,
    root: Path,
    filename: str,
    body: Mapping[str, object],
) -> Path:
    """Durably create one sanitized diagnostic receipt without overwriting.

    ``root`` must already be an existing private namespace.  It is never a
    journal or a package target.  A pre-existing leaf, including a symlink,
    is a collision and remains untouched.
    """

    if (
        not filename.startswith("diagnostic-")
        or not filename.endswith(".json")
        or "/" in filename
        or filename.startswith(".")
    ):
        raise RuntimeError("diagnostic receipt filename is invalid")
    _require_safe_directory(root)
    unsigned = dict(body)
    body_sha256 = canonical_sha256(unsigned)
    body_hex = body_sha256[7:]
    if filename != f"diagnostic-{body_hex}.json":
        raise RuntimeError("diagnostic receipt filename is not body-bound")
    document = dict(unsigned)
    document["receipt_sha256"] = body_sha256
    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    try:
        root_info = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_IMODE(root_info.st_mode) != 0o700
            or (root_info.st_uid, root_info.st_gid) != (os.geteuid(), os.getegid())
        ):
            raise RuntimeError("diagnostic receipt directory mode drifts")
        pending = f".{body_hex}.pending"
        existing_names = os.listdir(root_fd)
        if pending in existing_names:
            _repair_current_pending(root_fd, pending, payload)
            existing_names = os.listdir(root_fd)
        for existing in existing_names:
            if re.fullmatch(r"\.[0-9a-f]{64}\.pending", existing):
                _recover_pending(root_fd, existing)
                continue
            _require_receipt_entry(root_fd, existing)
        if filename in os.listdir(root_fd):
            if _read_regular(root_fd, filename) != payload:
                raise RuntimeError("diagnostic receipt collision")
            return _resolved_root_path(root, root_info) / filename
        fd = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        opened = os.fstat(fd)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        info, observed = _snapshot_regular(root_fd, pending)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_IMODE(info.st_mode) != 0o600
            or observed != payload
        ):
            raise RuntimeError("diagnostic receipt write verification failed")
        _recover_pending(root_fd, pending)
        if _read_regular(root_fd, filename) != payload:
            raise RuntimeError("diagnostic receipt publication verification failed")
        return _resolved_root_path(root, root_info) / filename
    finally:
        os.close(root_fd)


def _snapshot_regular(root_fd: int, name: str) -> tuple[os.stat_result, bytes]:
    before = _lstat_at(root_fd, name)
    if stat.S_ISLNK(before.st_mode):
        raise RuntimeError("diagnostic receipt entry is unsafe")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
    try:
        info = os.fstat(fd)
        if not _same_inode(before, info):
            raise RuntimeError("diagnostic receipt entry drifted during snapshot")
        payload = b""
        while chunk := os.read(fd, 1024 * 1024):
            payload += chunk
        after_fd = os.fstat(fd)
        after_path = _lstat_at(root_fd, name)
        if (
            not stat.S_ISREG(after_fd.st_mode)
            or not _same_inode(before, after_fd)
            or not _same_inode(before, after_path)
            or stat.S_IMODE(after_fd.st_mode) != 0o600
            or (after_fd.st_uid, after_fd.st_gid) != (os.geteuid(), os.getegid())
        ):
            raise RuntimeError("diagnostic receipt entry drifted during snapshot")
        return after_fd, payload
    finally:
        os.close(fd)


def _read_regular(root_fd: int, name: str) -> bytes:
    return _snapshot_regular(root_fd, name)[1]


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare the identity and regular-file contract we rely on for ownership."""

    return (
        (
            left.st_dev,
            left.st_ino,
            left.st_mode,
            left.st_size,
            left.st_mtime_ns,
            left.st_ctime_ns,
        )
        == (
            right.st_dev,
            right.st_ino,
            right.st_mode,
            right.st_size,
            right.st_mtime_ns,
            right.st_ctime_ns,
        )
    )


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Directories legitimately change size as entries are linked/unlinked."""

    return (left.st_dev, left.st_ino, left.st_mode) == (right.st_dev, right.st_ino, right.st_mode)


def _same_link_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare an inode across a hard-link operation, which changes ctime."""

    return (left.st_dev, left.st_ino, left.st_mode, left.st_size) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
    )


def _lstat_at(root_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=root_fd, follow_symlinks=False)


def _resolved_root_path(root: Path, expected: os.stat_result) -> Path:
    """Re-resolve the caller path and reject a renamed/replaced root directory."""

    try:
        resolved = root.resolve(strict=True)
        observed = os.lstat(resolved)
    except OSError as exc:
        raise RuntimeError("diagnostic receipt directory path drifted") from exc
    if not stat.S_ISDIR(observed.st_mode) or not _same_directory_identity(expected, observed):
        raise RuntimeError("diagnostic receipt directory path drifted")
    return resolved


def _require_receipt_entry(root_fd: int, name: str) -> None:
    if not re.fullmatch(r"diagnostic-[0-9a-f]{64}\.json", name):
        raise RuntimeError("diagnostic receipt inventory is unsafe")
    _, payload = _snapshot_regular(root_fd, name)
    try:
        document = json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("diagnostic receipt inventory is unsafe") from exc
    if not isinstance(document, dict):
        raise RuntimeError("diagnostic receipt inventory is unsafe")
    declared = document.pop("receipt_sha256", None)
    digest = canonical_sha256(document)
    if declared != digest or name != f"diagnostic-{digest[7:]}.json":
        raise RuntimeError("diagnostic receipt inventory is unsafe")


def _repair_current_pending(root_fd: int, name: str, expected_payload: bytes) -> None:
    """Resume only a crash-prefix of this exact receipt's deterministic pending."""

    info, observed = _snapshot_regular(root_fd, name)
    if observed == expected_payload:
        return
    if not observed or not expected_payload.startswith(observed):
        raise RuntimeError("diagnostic receipt pending collision")
    fd = os.open(name, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
    try:
        opened = os.fstat(fd)
        if not _same_inode(info, opened):
            raise RuntimeError("diagnostic receipt pending ownership drifted")
        os.ftruncate(fd, 0)
        view = memoryview(expected_payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    repaired, payload = _snapshot_regular(root_fd, name)
    if (
        (info.st_dev, info.st_ino, info.st_mode)
        != (repaired.st_dev, repaired.st_ino, repaired.st_mode)
        or payload != expected_payload
    ):
        raise RuntimeError("diagnostic receipt pending repair verification failed")


def _recover_pending(root_fd: int, name: str) -> None:
    """Finish a durable pending receipt or reject partial/foreign state."""

    try:
        pending_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
    except OSError as exc:
        raise RuntimeError("diagnostic receipt pending ownership drifted") from exc
    try:
        pending_info = os.fstat(pending_fd)
        if (
            not stat.S_ISREG(pending_info.st_mode)
            or stat.S_IMODE(pending_info.st_mode) != 0o600
            or (pending_info.st_uid, pending_info.st_gid) != (os.geteuid(), os.getegid())
        ):
            raise RuntimeError("diagnostic receipt pending ownership drifted")
        chunks: list[bytes] = []
        while chunk := os.read(pending_fd, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        try:
            document = json.loads(payload)
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("diagnostic receipt pending payload is invalid") from exc
        if not isinstance(document, dict):
            raise RuntimeError("diagnostic receipt pending payload is invalid")
        declared = document.pop("receipt_sha256", None)
        digest = canonical_sha256(document)
        expected_pending = f".{digest[7:]}.pending"
        target = f"diagnostic-{digest[7:]}.json"
        if declared != digest or name != expected_pending:
            raise RuntimeError("diagnostic receipt pending payload is invalid")
        try:
            source_before_link = _lstat_at(root_fd, name)
        except OSError as exc:
            raise RuntimeError("diagnostic receipt pending ownership drifted") from exc
        if not _same_inode(pending_info, source_before_link):
            raise RuntimeError("diagnostic receipt pending ownership drifted")
        created_target: os.stat_result | None = None
        try:
            os.link(name, target, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
        except FileExistsError:
            target_info, target_payload = _snapshot_regular(root_fd, target)
            if target_payload != payload or not _same_link_identity(pending_info, target_info):
                raise RuntimeError("diagnostic receipt collision") from None
        else:
            try:
                target_info = _lstat_at(root_fd, target)
            except OSError as exc:
                raise RuntimeError("diagnostic receipt publication verification failed") from exc
            created_target = target_info
            target_info, target_payload = _snapshot_regular(root_fd, target)
            if not _same_link_identity(pending_info, target_info) or target_payload != payload:
                # Remove only the hard link made by this invocation.  The source
                # pending path is deliberately retained for forensic recovery.
                if _same_link_identity(created_target, _lstat_at(root_fd, target)):
                    os.unlink(target, dir_fd=root_fd)
                    os.fsync(root_fd)
                raise RuntimeError("diagnostic receipt pending ownership drifted")
        os.fsync(root_fd)
        source_info, source_payload = _snapshot_regular(root_fd, name)
        if not _same_link_identity(pending_info, source_info) or source_payload != payload:
            raise RuntimeError("diagnostic receipt pending ownership drifted")
        os.unlink(name, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(pending_fd)
