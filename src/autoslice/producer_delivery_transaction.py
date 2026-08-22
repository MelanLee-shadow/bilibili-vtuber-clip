"""Candidate-private prepare and short-lease delivery materialization.

This module intentionally owns only the Talk/Song producer artifact boundary.
It is not a generic state transition framework: a caller supplies the exact
candidate artifacts and, later, its lane-specific state transaction.  Preparing
never writes a public delivery target.  Materialization requires the canonical
``RunnerCommitLease`` and is resumable from its small, artifact-only journal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.autoslice.qixi_transaction_core import (
    RunnerCommitLease,
    require_runner_commit_lease,
)
from src.autoslice.repository_asset_authority import (
    DEPLOYED_AUTHORITY_MANIFEST_SCHEMA,
    _canonical_sha256,
)


PREPARED_DELIVERY_SCHEMA = "producer-prepared-delivery.v1"
DELIVERY_JOURNAL_SCHEMA = "producer-delivery-journal.v1"


class ProducerDeliveryTransactionError(RuntimeError):
    """A candidate-private producer delivery cannot safely proceed."""


@dataclass(frozen=True, slots=True)
class DeliveryArtifact:
    role: str
    source: Path
    target: Path
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    runtime_root: Path
    lane: str
    candidate_id: str
    prepared_sha256: str
    manifest_path: Path


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _regular_snapshot(path: Path) -> dict[str, int | str]:
    _safe_directory(path.absolute().parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"cannot open regular artifact: {path}") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ProducerDeliveryTransactionError(f"artifact is not regular: {path}")
        digest = _sha256_fd(descriptor)
        live = os.lstat(path)
        if (live.st_dev, live.st_ino) != (observed.st_dev, observed.st_ino) or not stat.S_ISREG(live.st_mode):
            raise ProducerDeliveryTransactionError(f"artifact identity changed while read: {path}")
        return {
            "sha256": digest,
            "device": observed.st_dev,
            "inode": observed.st_ino,
            "mode": stat.S_IMODE(observed.st_mode),
            "size": observed.st_size,
        }
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    return str(_regular_snapshot(path)["sha256"])


def _read_regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"cannot open regular document: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ProducerDeliveryTransactionError(f"document is not regular: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        live = os.lstat(path)
        if not stat.S_ISREG(live.st_mode) or (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino):
            raise ProducerDeliveryTransactionError(f"document identity changed while read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _canonical_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _without_digest(document: dict) -> dict:
    normalized = dict(document)
    normalized.pop("prepared_sha256", None)
    return normalized


def _normalized_sha256(value: str) -> str:
    raw = value.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise ProducerDeliveryTransactionError("expected sha256 is malformed")
    return raw


def _safe_component(value: str, *, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise ProducerDeliveryTransactionError(f"unsafe {label}")
    return value


def _safe_directory(path: Path) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            ancestor = os.lstat(current)
        except OSError as exc:
            raise ProducerDeliveryTransactionError(f"private directory unavailable: {path}") from exc
        if stat.S_ISLNK(ancestor.st_mode):
            raise ProducerDeliveryTransactionError(f"private directory ancestor unsafe: {path}")
    try:
        observed = os.lstat(absolute)
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"private directory unavailable: {path}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise ProducerDeliveryTransactionError(f"private directory unsafe: {path}")
    return absolute


def _private_directory(root: Path, *parts: str) -> Path:
    """Create private descendants one component at a time, never via a link."""

    current = root
    for part in parts:
        if not re.fullmatch(r"[.A-Za-z0-9_-]{1,96}", part) or part in {".", ".."}:
            raise ProducerDeliveryTransactionError("unsafe private path component")
        child = current / part
        try:
            observed = os.lstat(child)
        except FileNotFoundError:
            try:
                os.mkdir(child, 0o700)
            except OSError as exc:
                raise ProducerDeliveryTransactionError(f"cannot create private directory: {child}") from exc
            observed = os.lstat(child)
            if stat.S_IMODE(observed.st_mode) != 0o700:
                raise ProducerDeliveryTransactionError(f"private directory mode unsafe: {child}")
        except OSError as exc:
            raise ProducerDeliveryTransactionError(f"private directory unavailable: {child}") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise ProducerDeliveryTransactionError(f"private directory unsafe: {child}")
        if stat.S_IMODE(observed.st_mode) != 0o700:
            raise ProducerDeliveryTransactionError(f"private directory mode unsafe: {child}")
        current = child
    return current.resolve(strict=True)


def _under(path: Path, root: Path, *, label: str) -> Path:
    absolute = path.absolute()
    _safe_directory(absolute.parent)
    try:
        raw = os.lstat(absolute)
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(raw.st_mode):
        raise ProducerDeliveryTransactionError(f"{label} is a symlink")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"{label} is unavailable: {path}") from exc
    if not resolved.is_relative_to(root):
        raise ProducerDeliveryTransactionError(f"{label} escapes runtime root")
    return resolved


def _copy_regular(
    source: Path, destination: Path, expected: str, source_snapshot: dict[str, int | str],
) -> dict[str, int | str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"cannot open source: {source}") from exc
    temporary: Path | None = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ProducerDeliveryTransactionError(f"source is not regular: {source}")
        source_identity = {
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
            "mode": stat.S_IMODE(source_stat.st_mode),
            "size": source_stat.st_size,
        }
        if source_identity != {key: source_snapshot[key] for key in source_identity}:
            raise ProducerDeliveryTransactionError(f"source generation drift: {source}")
        expected_digest = _normalized_sha256(expected)
        if _sha256_fd(source_fd) != expected_digest:
            raise ProducerDeliveryTransactionError(f"source hash drift: {source}")
        os.lseek(source_fd, 0, os.SEEK_SET)
        descriptor, name = tempfile.mkstemp(prefix=".prepare-", dir=destination.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(source_fd, "rb", closefd=False) as input_file, os.fdopen(descriptor, "wb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        copied_digest = _sha256_file(temporary)
        if copied_digest != expected_digest:
            raise ProducerDeliveryTransactionError(f"prepared copy hash drift: {source}")
        if _regular_snapshot(source) != {**source_identity, "sha256": expected_digest}:
            raise ProducerDeliveryTransactionError(f"source identity changed: {source}")
        os.replace(temporary, destination)
        temporary = None
        staged_stat = os.lstat(destination)
        if not stat.S_ISREG(staged_stat.st_mode) or stat.S_IMODE(staged_stat.st_mode) != 0o600:
            raise ProducerDeliveryTransactionError(f"prepared copy mode invalid: {destination}")
        return {
            "source_path": str(source),
            "source_sha256": f"sha256:{expected_digest}",
            "source_device": source_stat.st_dev,
            "source_inode": source_stat.st_ino,
            "source_mode": stat.S_IMODE(source_stat.st_mode),
            "source_size": source_stat.st_size,
            "staged_path": str(destination),
            "staged_sha256": f"sha256:{copied_digest}",
            "staged_device": staged_stat.st_dev,
            "staged_inode": staged_stat.st_ino,
            "staged_mode": stat.S_IMODE(staged_stat.st_mode),
            "staged_size": staged_stat.st_size,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(source_fd)


def _write_create_only(path: Path, payload: bytes) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise ProducerDeliveryTransactionError(f"create-only document collision: {path}") from exc
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o600
            or _read_regular_bytes(path) != payload
        ):
            raise ProducerDeliveryTransactionError(f"create-only document collision: {path}")
        return False
    except OSError as exc:
        raise ProducerDeliveryTransactionError(f"cannot create private document: {path}") from exc
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    observed = os.lstat(path)
    if not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o600:
        raise ProducerDeliveryTransactionError(f"private document mode invalid: {path}")
    return True


def _runtime_root(path: Path) -> Path:
    return _safe_directory(path)


def _nearest_trusted_parent(root: Path, path: Path) -> tuple[Path, list[str]]:
    """Return existing non-link ancestor and missing descendant components."""

    missing: list[str] = []
    current = path
    while True:
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            observed = None
        except OSError as exc:
            raise ProducerDeliveryTransactionError("delivery target parent is unavailable") from exc
        if observed is not None:
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
                raise ProducerDeliveryTransactionError("delivery target parent is unsafe")
            break
        if current == root or not current.is_relative_to(root):
            raise ProducerDeliveryTransactionError("delivery target parent escapes runtime root")
        missing.append(current.name)
        current = current.parent
    current = _safe_directory(current)
    if current.stat().st_dev != root.stat().st_dev:
        raise ProducerDeliveryTransactionError("prepared delivery crosses filesystems")
    return current, list(reversed(missing))


def _materialize_target_parent(root: Path, path: Path) -> Path:
    current, missing = _nearest_trusted_parent(root, path)
    for part in missing:
        child = current / part
        try:
            os.mkdir(child, 0o755)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProducerDeliveryTransactionError(f"cannot create delivery parent: {child}") from exc
        current = _safe_directory(child)
        if current.stat().st_dev != root.stat().st_dev:
            raise ProducerDeliveryTransactionError("delivery target crosses filesystems")
    return current


def deployment_authority_binding(runtime_root: Path) -> dict[str, str]:
    """Capture the deployed repository seal used by a prepared candidate."""

    root = _runtime_root(runtime_root)
    repo = _safe_directory(root / "repo")
    commit = _read_regular_bytes(repo / "DEPLOYED_COMMIT")
    manifest = _read_regular_bytes(repo / "DEPLOYED_AUTHORITY_MANIFEST.json")
    try:
        commit_text = commit.decode("utf-8", errors="strict").split(maxsplit=1)[0]
        document = json.loads(manifest.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, IndexError, json.JSONDecodeError) as exc:
        raise ProducerDeliveryTransactionError("deployed authority seal is invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit_text) or not isinstance(document, dict):
        raise ProducerDeliveryTransactionError("deployed authority seal is invalid")
    body = dict(document)
    declared = body.pop("manifest_sha256", None)
    if (
        set(body) != {"schema_version", "deployed_commit", "entries"}
        or body.get("schema_version") != DEPLOYED_AUTHORITY_MANIFEST_SCHEMA
        or body.get("deployed_commit") != commit_text
        or not isinstance(body.get("entries"), dict)
        or not body["entries"]
        or not isinstance(declared, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", declared)
        or _canonical_sha256(body) != declared
    ):
        raise ProducerDeliveryTransactionError("deployed authority manifest is invalid")
    for relative, entry in body["entries"].items():
        candidate = Path(relative) if isinstance(relative, str) else None
        if (
            candidate is None
            or candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.as_posix() != relative
            or not isinstance(entry, dict)
            or set(entry) != {"bytes", "sha256"}
            or isinstance(entry.get("bytes"), bool)
            or not isinstance(entry.get("bytes"), int)
            or entry["bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry["sha256"])
        ):
            raise ProducerDeliveryTransactionError("deployed authority manifest entries are invalid")
    return {
        "commit_bytes_sha256": "sha256:" + hashlib.sha256(commit).hexdigest(),
        "commit": commit_text,
        "authority_manifest_sha256": "sha256:" + hashlib.sha256(manifest).hexdigest(),
    }


def prepare_delivery(
    *,
    runtime_root: Path,
    lane: str,
    candidate_id: str,
    artifacts: Iterable[DeliveryArtifact],
    deployed_authority: dict[str, str] | None = None,
) -> PreparedDelivery:
    """Copy verified candidate artifacts into a private, create-only manifest.

    The returned handle has no public delivery effects.  All target locations
    are recorded exactly and must be below ``runtime_root``.
    """

    root = _runtime_root(runtime_root)
    lane = _safe_component(lane, label="lane")
    candidate_id = _safe_component(candidate_id, label="candidate id")
    authority = deployed_authority or deployment_authority_binding(root)
    if not isinstance(authority, dict) or set(authority) != {
        "commit_bytes_sha256", "commit", "authority_manifest_sha256"
    } or not all(isinstance(value, str) and value for value in authority.values()):
        raise ProducerDeliveryTransactionError("deployed authority binding is invalid")
    rows = list(artifacts)
    if not rows:
        raise ProducerDeliveryTransactionError("prepared delivery has no artifacts")
    normal_rows: list[tuple[DeliveryArtifact, Path, Path, str, dict[str, int | str]]] = []
    targets: set[Path] = set()
    for row in rows:
        role = _safe_component(row.role, label="artifact role")
        source = _under(row.source, root, label="source")
        target = row.target.absolute()
        if target in targets or target.name.startswith("."):
            raise ProducerDeliveryTransactionError("duplicate or hidden delivery target")
        if ".." in target.parts or not target.parent.is_relative_to(root):
            raise ProducerDeliveryTransactionError("delivery target escapes runtime root")
        # Preparing is read/copy-only with respect to shared delivery.  A
        # missing dated delivery directory is created only by the later lease
        # holder, never by a provider/build worker.
        _nearest_trusted_parent(root, target.parent)
        targets.add(target)
        normal_rows.append((
            DeliveryArtifact(role, source, target, row.expected_sha256), source,
            target, _normalized_sha256(row.expected_sha256), _regular_snapshot(source),
        ))

    seed = {
        "schema_version": PREPARED_DELIVERY_SCHEMA,
        "lane": lane,
        "candidate_id": candidate_id,
        "deployed_authority": authority,
        "targets": [
            {
                "role": row.role, "source": str(source), "target": str(target),
                "source_sha256": f"sha256:{digest}",
                "source_device": snapshot["device"],
                "source_inode": snapshot["inode"],
                "source_mode": snapshot["mode"],
                "source_size": snapshot["size"],
            }
            for row, source, target, digest, snapshot in normal_rows
        ],
    }
    digest = hashlib.sha256(_canonical_bytes(seed)).hexdigest()
    private_root = _private_directory(root, ".prepared-deliveries", lane, candidate_id, digest)
    manifest_path = private_root / "prepared.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        loaded = load_prepared_delivery(runtime_root=root, manifest_path=manifest_path)
        if loaded.lane != lane or loaded.candidate_id != candidate_id:
            raise ProducerDeliveryTransactionError("prepared delivery collision")
        return loaded

    entries: list[dict[str, object]] = []
    try:
        for index, (row, source, target, expected, snapshot) in enumerate(normal_rows):
            staged = private_root / f"{index:02d}-{row.role}"
            entry = _copy_regular(source, staged, expected, snapshot)
            entry.update({"role": row.role, "target_path": str(target), "target_absent": True})
            entries.append(entry)
        document = {
            "schema_version": PREPARED_DELIVERY_SCHEMA,
            "lane": lane,
            "candidate_id": candidate_id,
            "deployed_authority": authority,
            "upload_enabled": False,
            "artifacts": entries,
        }
        prepared_sha = hashlib.sha256(_canonical_bytes(document)).hexdigest()
        document["prepared_sha256"] = f"sha256:{prepared_sha}"
        _write_create_only(manifest_path, _canonical_bytes(document))
        _fsync_directory(private_root)
        return PreparedDelivery(root, lane, candidate_id, prepared_sha, manifest_path)
    except BaseException:
        # A failed preparation is never a committable handle.  Preserve no
        # partial manifest; ordinary staged bytes are candidate-private.
        manifest_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_prepared_delivery(*, runtime_root: Path, manifest_path: Path, allow_materialized: bool = False) -> PreparedDelivery:
    root = _runtime_root(runtime_root)
    resolved = _under(manifest_path, root, label="prepared manifest")
    relative = resolved.relative_to(root)
    if (
        resolved.name != "prepared.json"
        or resolved.is_symlink()
        or len(relative.parts) != 5
        or relative.parts[0] != ".prepared-deliveries"
        or not re.fullmatch(r"[0-9a-f]{64}", relative.parts[3])
    ):
        raise ProducerDeliveryTransactionError("prepared manifest is unsafe")
    manifest_stat = os.lstat(resolved)
    if not stat.S_ISREG(manifest_stat.st_mode) or stat.S_IMODE(manifest_stat.st_mode) != 0o600:
        raise ProducerDeliveryTransactionError("prepared manifest mode is unsafe")
    for component in (root / relative.parts[0], root / relative.parts[0] / relative.parts[1], root / relative.parts[0] / relative.parts[1] / relative.parts[2], resolved.parent):
        directory_stat = os.lstat(component)
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode) or stat.S_IMODE(directory_stat.st_mode) != 0o700:
            raise ProducerDeliveryTransactionError("prepared manifest namespace is unsafe")
    try:
        document = json.loads(_read_regular_bytes(resolved).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerDeliveryTransactionError("prepared manifest is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != PREPARED_DELIVERY_SCHEMA:
        raise ProducerDeliveryTransactionError("prepared manifest schema drifts")
    lane = _safe_component(str(document.get("lane") or ""), label="lane")
    candidate_id = _safe_component(str(document.get("candidate_id") or ""), label="candidate id")
    if relative.parts[1:3] != (lane, candidate_id):
        raise ProducerDeliveryTransactionError("prepared manifest namespace drifts")
    if set(document) != {
        "schema_version", "lane", "candidate_id", "deployed_authority",
        "upload_enabled", "artifacts", "prepared_sha256",
    } or document.get("upload_enabled") is not False:
        raise ProducerDeliveryTransactionError("prepared manifest schema drifts")
    expected = document.get("prepared_sha256")
    if not isinstance(expected, str):
        raise ProducerDeliveryTransactionError("prepared manifest lacks digest")
    actual = hashlib.sha256(_canonical_bytes(_without_digest(document))).hexdigest()
    if expected != f"sha256:{actual}":
        raise ProducerDeliveryTransactionError("prepared manifest digest drifts")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ProducerDeliveryTransactionError("prepared manifest has no artifacts")
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise ProducerDeliveryTransactionError("prepared manifest artifact is invalid")
        if set(entry) != {
            "role", "source_path", "source_sha256", "source_device", "source_inode",
            "source_mode", "source_size", "staged_path", "staged_sha256",
            "staged_device", "staged_inode", "staged_mode", "staged_size",
            "target_path", "target_absent",
        }:
            raise ProducerDeliveryTransactionError("prepared manifest artifact schema drifts")
        if (
            not isinstance(entry["role"], str)
            or not isinstance(entry["source_path"], str)
            or not isinstance(entry["target_path"], str)
            or entry["target_absent"] is not True
            or any(
                isinstance(entry[key], bool) or not isinstance(entry[key], int) or entry[key] < 0
                for key in (
                    "source_device", "source_inode", "source_mode", "source_size",
                    "staged_device", "staged_inode", "staged_mode", "staged_size",
                )
            )
            or not isinstance(entry["source_sha256"], str)
            or not isinstance(entry["staged_sha256"], str)
        ):
            raise ProducerDeliveryTransactionError("prepared manifest artifact types drift")
        staged_value = Path(str(entry.get("staged_path") or "")).absolute()
        target = Path(str(entry.get("target_path") or "")).absolute()
        if not target.parent.is_relative_to(root) or not entry.get("target_absent"):
            raise ProducerDeliveryTransactionError("prepared target authority drifts")
        if staged_value.is_relative_to(root) and staged_value.exists():
            staged = _under(staged_value, root, label="staged artifact")
            if _sha256_file(staged) != _normalized_sha256(str(entry.get("staged_sha256") or "")):
                raise ProducerDeliveryTransactionError("prepared staged artifact drifts")
        elif not allow_materialized:
            raise ProducerDeliveryTransactionError("prepared staged artifact is unavailable")
    authority = document.get("deployed_authority")
    if not isinstance(authority, dict):
        raise ProducerDeliveryTransactionError("prepared deployed authority is invalid")
    seed = {
        "schema_version": PREPARED_DELIVERY_SCHEMA,
        "lane": lane,
        "candidate_id": candidate_id,
        "deployed_authority": authority,
        "targets": [
            {
                "role": entry["role"], "source": entry["source_path"],
                "target": entry["target_path"], "source_sha256": entry["source_sha256"],
                "source_device": entry["source_device"],
                "source_inode": entry["source_inode"],
                "source_mode": entry["source_mode"],
                "source_size": entry["source_size"],
            }
            for entry in artifacts
        ],
    }
    if hashlib.sha256(_canonical_bytes(seed)).hexdigest() != relative.parts[3]:
        raise ProducerDeliveryTransactionError("prepared manifest namespace binding drifts")
    return PreparedDelivery(root, lane, candidate_id, actual, resolved)


def _read_document(handle: PreparedDelivery, *, allow_materialized: bool = False) -> dict:
    loaded = load_prepared_delivery(
        runtime_root=handle.runtime_root, manifest_path=handle.manifest_path,
        allow_materialized=allow_materialized,
    )
    if loaded != handle:
        raise ProducerDeliveryTransactionError("prepared handle authority drifts")
    return json.loads(_read_regular_bytes(handle.manifest_path).decode("utf-8"))


def _checked_artifacts(
    document: dict, *, runtime_root: Path, allow_installed: bool,
) -> list[tuple[dict, Path, Path, str]]:
    checked: list[tuple[dict, Path, Path, str]] = []
    for entry in document["artifacts"]:
        source = Path(str(entry["source_path"]))
        staged = Path(str(entry["staged_path"]))
        target = Path(str(entry["target_path"]))
        expected = _normalized_sha256(str(entry["staged_sha256"]))
        # This is intentionally repeated under the lease immediately before
        # formal journal creation.  A replaced target parent must fail without
        # minting a journal whose replay would look authoritative.
        _nearest_trusted_parent(runtime_root, target.parent)
        source_snapshot = _regular_snapshot(source)
        expected_source = (
            entry["source_device"], entry["source_inode"], entry["source_mode"], entry["source_size"],
        )
        actual_source = tuple(source_snapshot[key] for key in ("device", "inode", "mode", "size"))
        if actual_source != expected_source:
            raise ProducerDeliveryTransactionError("source preimage drifts")
        if source_snapshot["sha256"] != _normalized_sha256(str(entry["source_sha256"])):
            raise ProducerDeliveryTransactionError("source hash preimage drifts")
        if target.exists() or target.is_symlink():
            target_stat = os.lstat(target)
            owned_target = (
                stat.S_ISREG(target_stat.st_mode)
                and (target_stat.st_dev, target_stat.st_ino)
                == (entry["staged_device"], entry["staged_inode"])
            )
            if not allow_installed or not owned_target or _sha256_file(target) != expected:
                raise ProducerDeliveryTransactionError("delivery target is no longer absent")
        else:
            staged_snapshot = _regular_snapshot(staged)
            actual_staged = tuple(staged_snapshot[key] for key in ("device", "inode", "mode", "size"))
            expected_staged = (
                entry["staged_device"], entry["staged_inode"],
                entry["staged_mode"], entry["staged_size"],
            )
            if actual_staged != expected_staged or staged_snapshot["sha256"] != expected:
                raise ProducerDeliveryTransactionError("staged preimage drifts")
        checked.append((entry, staged, target, expected))
    return checked


def _journal_payload(*, handle: PreparedDelivery, artifacts: list[dict], status: str = "PREPARED", installed_roles: list[str] | None = None) -> dict:
    document = {
        "schema_version": DELIVERY_JOURNAL_SCHEMA,
        "status": status,
        "prepared_manifest": str(handle.manifest_path),
        "prepared_sha256": f"sha256:{handle.prepared_sha256}",
        "artifacts": [
            {
                "role": entry["role"], "staged_path": entry["staged_path"],
                "staged_device": entry["staged_device"], "staged_inode": entry["staged_inode"],
                "target_path": entry["target_path"], "sha256": entry["staged_sha256"],
            }
            for entry in artifacts
        ],
        "installed_roles": installed_roles or [],
        "upload_enabled": False,
    }
    document["journal_sha256"] = "sha256:" + hashlib.sha256(_canonical_bytes(document)).hexdigest()
    return document


def _read_journal(path: Path, *, handle: PreparedDelivery, artifacts: list[dict]) -> dict:
    observed = os.lstat(path)
    if not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o600:
        raise ProducerDeliveryTransactionError("delivery journal mode is unsafe")
    try:
        journal = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProducerDeliveryTransactionError("delivery journal is invalid") from exc
    if (
        not isinstance(journal, dict)
        or set(journal) != {
            "schema_version", "status", "prepared_manifest", "prepared_sha256",
            "artifacts", "installed_roles", "upload_enabled", "journal_sha256",
        }
        or journal.get("schema_version") != DELIVERY_JOURNAL_SCHEMA
        or journal.get("prepared_manifest") != str(handle.manifest_path)
        or journal.get("prepared_sha256") != f"sha256:{handle.prepared_sha256}"
        or journal.get("upload_enabled") is not False
        or journal.get("status") not in {"PREPARED", "INSTALLING", "COMMITTED"}
    ):
        raise ProducerDeliveryTransactionError("delivery journal authority drifts")
    expected_digest = journal.get("journal_sha256")
    if not isinstance(expected_digest, str):
        raise ProducerDeliveryTransactionError("delivery journal digest is missing")
    unsigned = dict(journal)
    unsigned.pop("journal_sha256", None)
    if expected_digest != "sha256:" + hashlib.sha256(_canonical_bytes(unsigned)).hexdigest():
        raise ProducerDeliveryTransactionError("delivery journal digest drifts")
    expected_inventory = _journal_payload(handle=handle, artifacts=artifacts)["artifacts"]
    if journal.get("artifacts") != expected_inventory:
        raise ProducerDeliveryTransactionError("delivery journal inventory drifts")
    roles = journal.get("installed_roles")
    expected_roles = [str(entry["role"]) for entry in artifacts]
    if (
        not isinstance(roles, list)
        or any(not isinstance(role, str) for role in roles)
        or roles != expected_roles[:len(roles)]
        or (journal["status"] == "PREPARED" and roles)
        or (journal["status"] == "INSTALLING" and not roles)
        or (journal["status"] == "COMMITTED" and roles != expected_roles)
    ):
        raise ProducerDeliveryTransactionError("delivery journal checkpoints drift")
    return journal


def commit_prepared_delivery(*, handle: PreparedDelivery, lease: RunnerCommitLease) -> dict[str, object]:
    """Install prepared bytes under a short lease; journal supports replay.

    This artifact-only primitive deliberately does not change state.  Lane B
    will bind its state after-image to this same journal before exposing the
    full runner path.
    """

    require_runner_commit_lease(lease, runtime_root=handle.runtime_root)
    possible_journal = handle.runtime_root / ".delivery-journal" / f"{handle.lane}-{handle.candidate_id}-{handle.prepared_sha256}.json"
    document = _read_document(handle, allow_materialized=possible_journal.exists())
    if document.get("deployed_authority") != deployment_authority_binding(handle.runtime_root):
        raise ProducerDeliveryTransactionError("deployed authority preimage drifts")
    journal_root = handle.runtime_root / ".delivery-journal"
    journal_path = journal_root / f"{handle.lane}-{handle.candidate_id}-{handle.prepared_sha256}.json"
    artifacts = document["artifacts"]
    journal = _journal_payload(handle=handle, artifacts=artifacts)
    if journal_root.exists():
        journal_stat = os.lstat(_safe_directory(journal_root))
        if stat.S_IMODE(journal_stat.st_mode) != 0o700:
            raise ProducerDeliveryTransactionError("delivery journal namespace mode is unsafe")
    if journal_path.exists() or journal_path.is_symlink():
        journal = _read_journal(journal_path, handle=handle, artifacts=artifacts)
        # A journal is owned recovery authority.  Its installed targets may be
        # present after a crash; no unrelated pre-existing target is accepted.
        checked = _checked_artifacts(
            document, runtime_root=handle.runtime_root, allow_installed=True,
        )
    else:
        # Every independent source/stage/target preimage is checked before
        # the first formal journal inode.  A stale prepare therefore writes
        # neither journal nor delivery target.
        checked = _checked_artifacts(
            document, runtime_root=handle.runtime_root, allow_installed=False,
        )
        journal_root = _private_directory(handle.runtime_root, ".delivery-journal")
        journal_path = journal_root / journal_path.name
        _write_create_only(journal_path, _canonical_bytes(journal))
        _fsync_directory(journal_root)
    installed: list[dict[str, str]] = []
    for entry, staged, target, expected in checked:
        if not target.exists():
            _materialize_target_parent(handle.runtime_root, target.parent)
            os.replace(staged, target)
            _fsync_directory(target.parent)
        if _sha256_file(target) != expected:
            raise ProducerDeliveryTransactionError("installed target hash drifts")
        installed.append({"role": str(entry["role"]), "path": str(target), "sha256": f"sha256:{expected}"})
        journal = _journal_payload(
            handle=handle, artifacts=artifacts, status="INSTALLING",
            installed_roles=[row["role"] for row in installed],
        )
        _write_replace(journal_path, _canonical_bytes(journal))
        _fsync_directory(journal_root)
    committed = _journal_payload(
        handle=handle, artifacts=artifacts, status="COMMITTED",
        installed_roles=[row["role"] for row in installed],
    )
    _write_replace(journal_path, _canonical_bytes(committed))
    _fsync_directory(journal_root)
    return {"journal_path": str(journal_path), "artifacts": installed, "upload_enabled": False}


def _write_replace(path: Path, payload: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
