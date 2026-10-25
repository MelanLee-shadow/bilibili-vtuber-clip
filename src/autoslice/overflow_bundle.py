"""Portable, bounded handoff for candidate-private producer preparation.

The normal producer handle contains device and inode observations.  Copying
``prepared.json`` to another machine therefore cannot make a valid handle:
the staged bytes have new inodes, and a worker may have a different physical
filesystem.  This module carries the exact bytes and the logical path map in
an explicit bundle.  The controller materializes only the allowlisted source
files and calls :func:`prepare_delivery` again, producing a fresh handle.

The first implementation deliberately keeps one logical absolute
``runtime_root`` on both sides.  The worker may mount that root at a different
physical device, but the paths in the producer result remain unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    PreparedDelivery,
    ProducerDeliveryTransactionError,
    _canonical_bytes,
    _read_document,
    deployment_authority_binding,
    _deployment_repository,
    load_prepared_delivery,
    prepare_delivery,
)


OVERFLOW_BUNDLE_SCHEMA = "autoslice-overflow-bundle.v2"
BUNDLE_FILENAME = "bundle.json"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,192}\Z")
_FAILURE_FILE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_EXTERNAL_SOURCE_ROOTS = (
    Path("/opt/bilive/autoslice"),
    Path("/path/to/clouddrive2"),
)


class OverflowBundleError(ValueError):
    """A portable producer bundle is invalid or cannot be safely imported."""


# A descriptive alias is useful to callers that distinguish this path from
# ordinary producer delivery failures.
ProducerOverflowBundleError = OverflowBundleError


def _fail(message: str, cause: BaseException | None = None) -> None:
    if cause is None:
        raise OverflowBundleError(message)
    raise OverflowBundleError(message) from cause


def _canonical_hash(value: object, *, label: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        _fail(f"{label} must be a sha256 value")
    raw = value.removeprefix("sha256:")
    if _RAW_SHA256.fullmatch(raw) is None:
        _fail(f"{label} is not a sha256 value")
    return f"sha256:{raw}"


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        _fail(f"{label} is unsafe")
    return value


def _safe_runtime_root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute():
        _fail("runtime_root must be absolute")
    root = root.absolute()
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        try:
            observed = os.lstat(current)
        except OSError as exc:
            _fail("runtime_root is unavailable", exc)
        if stat.S_ISLNK(observed.st_mode):
            _fail("runtime_root has a symlink ancestor")
    try:
        observed = os.lstat(root)
    except OSError as exc:
        _fail("runtime_root is unavailable", exc)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        _fail("runtime_root is not a directory")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        _fail("runtime_root is unavailable", exc)


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail(f"{label} is unsafe")
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        _fail(f"{label} is unsafe")
    return path


def _relative_to_root(path: Path, root: Path, *, label: str) -> Path:
    if not path.is_absolute():
        _fail(f"{label} must be absolute")
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        _fail(f"{label} escapes runtime_root")
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        _fail(f"{label} is unsafe")
    current = root
    # A lexical prefix is insufficient when an attacker replaces ``out`` or
    # a candidate directory with a link.  Existing ancestors are checked one
    # component at a time; a missing parent is left for the create-only path
    # validator to create safely.
    for part in relative.parts[:-1]:
        current = current / part
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            _fail(f"{label} ancestor is unavailable", exc)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            _fail(f"{label} ancestor is unsafe")
    return _safe_relative(relative.as_posix(), label=label)


def _lstat_regular(path: Path, *, label: str) -> os.stat_result:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        _fail(f"{label} is unavailable", exc)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        _fail(f"{label} is not a regular file")
    return observed


def _read_regular(path: Path, *, label: str) -> bytes:
    _lstat_regular(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} cannot be opened", exc)
    try:
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            _fail(f"{label} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        live = os.lstat(path)
        if (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino):
            _fail(f"{label} changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _hash_regular(path: Path, *, label: str) -> tuple[str, int]:
    """Hash one regular file without retaining its payload in memory."""

    _lstat_regular(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} cannot be opened", exc)
    try:
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            _fail(f"{label} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        live = os.lstat(path)
        if (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino):
            _fail(f"{label} changed while read")
        return "sha256:" + digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _copy_regular_create_only(
    source: Path,
    destination: Path,
    *,
    root: Path,
    label: str,
    expected_sha256: str | None = None,
    expected_bytes: int | None = None,
) -> tuple[str, int]:
    """Copy a regular file create-only while hashing the stream.

    This keeps video/PCM payloads bounded by the chunk size.  If the
    destination already exists, it is accepted only when its streamed bytes
    have the expected digest and size.
    """

    _ensure_directory(destination.parent, root=root, label=f"{label} parent")
    _lstat_regular(source, label=f"{label} source")
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        _fail(f"{label} source cannot be opened", exc)
    source_stat = os.fstat(source_fd)
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        os.close(source_fd)
        _fail(f"{label} source is not a regular file")
    try:
        observed = os.lstat(destination)
    except FileNotFoundError:
        observed = None
    except OSError as exc:
        os.close(source_fd)
        _fail(f"{label} destination is unavailable", exc)
    if observed is not None:
        os.close(source_fd)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            _fail(f"{label} destination conflicts with a non-regular path")
        digest, size = _hash_regular(destination, label=f"{label} destination")
        if expected_sha256 is not None and digest != expected_sha256:
            _fail(f"{label} conflicts with existing bytes")
        if expected_bytes is not None and size != expected_bytes:
            _fail(f"{label} conflicts with existing bytes")
        return digest, size

    destination_fd: int | None = None
    created = False
    digest = hashlib.sha256()
    size = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            destination_fd = os.open(destination, flags, 0o600)
            created = True
        except FileExistsError:
            # A concurrent create is safe only if its complete bytes match.
            digest_value, size_value = _hash_regular(destination, label=f"{label} destination")
            if expected_sha256 is not None and digest_value != expected_sha256:
                _fail(f"{label} conflicts with existing bytes")
            if expected_bytes is not None and size_value != expected_bytes:
                _fail(f"{label} conflicts with existing bytes")
            return digest_value, size_value
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
            digest.update(chunk)
            size += len(chunk)
        os.fsync(destination_fd)
    except OSError as exc:
        _fail(f"{label} copy failed", exc)
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    live = os.lstat(source)
    if (live.st_dev, live.st_ino) != (source_stat.st_dev, source_stat.st_ino):
        if created:
            destination.unlink(missing_ok=True)
        _fail(f"{label} source changed while copied")
    digest_value = "sha256:" + digest.hexdigest()
    if expected_sha256 is not None and digest_value != expected_sha256:
        if created:
            destination.unlink(missing_ok=True)
        _fail(f"{label} hash drifts")
    if expected_bytes is not None and size != expected_bytes:
        if created:
            destination.unlink(missing_ok=True)
        _fail(f"{label} byte count drifts")
    return digest_value, size


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        count = os.write(descriptor, remaining)
        if not isinstance(count, int) or count <= 0:
            raise OSError("short write")
        remaining = remaining[count:]


def _ensure_directory(path: Path, *, root: Path, label: str, mode: int = 0o700) -> Path:
    """Create missing descendants without traversing a symlink."""

    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        _fail(f"{label} escapes runtime_root", exc)
    current = root
    for part in relative.parts:
        child = current / part
        try:
            observed = os.lstat(child)
        except FileNotFoundError:
            try:
                os.mkdir(child, mode)
            except OSError as exc:
                _fail(f"cannot create {label}", exc)
            observed = os.lstat(child)
        except OSError as exc:
            _fail(f"{label} is unavailable", exc)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            _fail(f"{label} contains an unsafe directory")
        current = child
    return current


def _create_only(path: Path, payload: bytes, *, root: Path, label: str) -> None:
    _ensure_directory(path.parent, root=root, label=f"{label} parent")
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        observed = None
    except OSError as exc:
        _fail(f"{label} is unavailable", exc)
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            _fail(f"{label} conflicts with a non-regular path")
        current = _read_regular(path, label=label)
        if current != payload:
            _fail(f"{label} conflicts with existing bytes")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _create_only(path, payload, root=root, label=label)
        return
    except OSError as exc:
        _fail(f"cannot create {label}", exc)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError as exc:
        try:
            os.close(descriptor)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        _fail(f"cannot write {label}", exc)
    else:
        os.close(descriptor)


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} is not an object")
    try:
        cloned = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        _fail(f"{label} is not JSON serializable", exc)
    if not isinstance(cloned, dict):
        _fail(f"{label} is not an object")
    return cloned


def _identity_matches_result(result: Mapping[str, object], *, job_id: str, attempt_id: str, release: str, input_sha: str) -> None:
    for key, expected in (
        ("job_id", job_id),
        ("attempt_id", attempt_id),
    ):
        if key in result and result.get(key) != expected:
            _fail(f"result {key} differs from bundle identity")
    for key, expected in (
        ("release_sha256", release),
        ("input_sha256", input_sha),
    ):
        if key in result:
            try:
                actual = _canonical_hash(result.get(key), label=f"result {key}")
            except OverflowBundleError:
                raise
            if actual != expected:
                _fail(f"result {key} differs from bundle identity")


def _prepared_reference(result: Mapping[str, object]) -> Mapping[str, object] | None:
    direct = result.get("prepared_delivery")
    if isinstance(direct, Mapping):
        return direct
    summary = result.get("prepared_summary")
    if isinstance(summary, Mapping) and isinstance(summary.get("prepared_delivery"), Mapping):
        return summary["prepared_delivery"]  # type: ignore[return-value]
    return None


def _prepared_context(
    *, runtime_root: Path, result: Mapping[str, object], status: object,
) -> tuple[PreparedDelivery | None, dict[str, Any] | None]:
    reference = _prepared_reference(result)
    is_prepare_status = status == "delivery_prepared_no_target"
    if reference is None:
        if is_prepare_status:
            _fail("successful prepare result has no prepared_delivery handle")
        return None, None
    if not is_prepare_status:
        _fail("failed or rejected result cannot carry a prepared_delivery handle")
    if not isinstance(reference.get("manifest_path"), str):
        _fail("prepared_delivery manifest_path is missing")
    manifest = Path(str(reference["manifest_path"]))
    if not manifest.is_absolute():
        _fail("prepared_delivery manifest_path must be absolute")
    try:
        handle = load_prepared_delivery(runtime_root=runtime_root, manifest_path=manifest)
        document = _read_document(handle)
    except (ProducerDeliveryTransactionError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("prepared_delivery handle cannot be validated", exc)
    if reference.get("prepared_sha256") != f"sha256:{handle.prepared_sha256}":
        _fail("prepared_delivery digest differs from manifest")
    if reference.get("upload_enabled") is not False:
        _fail("prepared_delivery upload policy is invalid")
    return handle, document


def _iter_strings(value: object, keys: tuple[str, ...] = ()):
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "profile_binding":
                # A validated config locator is task provenance, not a
                # candidate artifact to copy into the bundle. External
                # manifests remain explicit runtime inputs on both sides.
                from src.autoslice.channel_profile import normalize_profile_binding
                normalize_profile_binding(child)
                continue
            if isinstance(child, str) and (
                key in {"provider_endpoint_path", "provider_credential_source"}
                or str(key).endswith(("_dir", "_root"))
            ):
                # Directory namespaces, URL routes and credential provenance
                # are metadata, never candidate file capabilities. Preserve
                # their bytes without copying a directory or credential file.
                continue
            yield from _iter_strings(child, (*keys, str(key)))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_strings(child, keys)
    elif isinstance(value, str):
        yield value, keys


def _path_like(value: str) -> bool:
    return bool(value) and os.path.isabs(value) and not any(ch in value for ch in "\r\n\t")


def _source_binding_key(path: Path) -> Path:
    """Use the same lexical absolute spelling for result and request paths."""

    return path.absolute()


def _source_file_bindings(
    runtime_root: Path,
    source_files: object,
    *,
    label: str,
) -> tuple[dict[Path, dict[str, object]], list[dict[str, object]]]:
    """Validate request-side source preimages without copying their bytes."""

    if source_files is None:
        source_files = []
    if not isinstance(source_files, list):
        _fail(f"{label} must be a list")
    bindings: dict[Path, dict[str, object]] = {}
    normalized: list[dict[str, object]] = []
    allowed_roots = (runtime_root, *_EXTERNAL_SOURCE_ROOTS)
    safe_roots: list[Path] = []
    for candidate_root in allowed_roots:
        try:
            safe_root = _safe_runtime_root(candidate_root)
        except OverflowBundleError:
            # Optional production roots may not exist on a unit-test or
            # controller host.  A path under one is still rejected below.
            continue
        if safe_root not in safe_roots:
            safe_roots.append(safe_root)
    for index, raw in enumerate(source_files):
        if not isinstance(raw, Mapping) or set(raw) != {"path", "bytes", "sha256"}:
            _fail(f"{label}[{index}] schema is invalid")
        value = raw.get("path")
        if not isinstance(value, str) or not _path_like(value):
            _fail(f"{label}[{index}].path is invalid")
        path = Path(value)
        if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
            _fail(f"{label}[{index}].path is unsafe")
        absolute = _source_binding_key(path)
        matched = False
        for safe_root in safe_roots:
            try:
                _relative_to_root(absolute, safe_root, label=f"{label}[{index}].path")
            except OverflowBundleError:
                continue
            matched = True
            break
        if not matched:
            _fail(f"{label}[{index}].path is outside approved source roots")
        size = raw.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(f"{label}[{index}].bytes is invalid")
        expected = _canonical_hash(raw.get("sha256"), label=f"{label}[{index}].sha256")
        if absolute in bindings:
            _fail(f"{label}[{index}].path is duplicated")
        observed, observed_bytes = _hash_regular(absolute, label=f"{label}[{index}].path")
        if observed != expected or observed_bytes != size:
            _fail(f"{label}[{index}] source preimage drifts")
        binding = {"path": str(absolute), "bytes": observed_bytes, "sha256": observed}
        bindings[absolute] = binding
        normalized.append(binding)
    return bindings, normalized


def _target_allowed(relative: Path, *, date: str, target_root: tuple[str, ...] | None = None) -> bool:
    """Accept one profile delivery namespace without opening arbitrary repo data.

    The caller supplies the exact root derived from the bound profile and
    active deployment. Versioned producers write under the logical runtime;
    legacy deployments keep their delivery below ``repo``.
    """

    parts = relative.parts
    if len(parts) < 3:
        return False
    try:
        date_index = parts.index(date, 1)
    except ValueError:
        return False
    prefix = parts[:date_index]
    if not target_root or prefix != target_root or any(
        component in {"state", "queue", "queues", "logs", "credentials", "sourcecache"}
        for component in prefix
    ):
        return False
    return date_index < len(parts) - 1 and len(parts) == date_index + 2


def _bound_profile(
    runtime_root: Path, expected_profile_binding: object | None,
):
    """Load the caller-selected profile and verify its current manifest bytes."""

    if expected_profile_binding is None:
        _fail("expected_profile_binding is required; legacy unbound bundle is rejected")
    try:
        from src.autoslice.channel_profile import (
            binding_for_profile,
            load_channel_profile,
            normalize_profile_binding,
            require_profile_binding,
        )

        expected = normalize_profile_binding(expected_profile_binding)
        repo = _deployment_repository(runtime_root)
        profile = load_channel_profile(repo_root=repo, profile_binding=expected)
        actual = binding_for_profile(profile)
        require_profile_binding(actual, expected)
    except (OSError, TypeError, ValueError) as exc:
        _fail("bound channel profile is unavailable or differs from expected binding", exc)
    return profile, expected


def _bound_profile_target_root(runtime_root: Path, profile) -> tuple[str, ...]:
    """Derive the allowed delivery namespace from the already-bound profile."""

    try:
        target = profile.delivery_root
        relative = target.absolute().relative_to(runtime_root)
    except (AttributeError, OSError, ValueError) as exc:
        _fail("bound channel profile target root is invalid", exc)
    if not relative.parts:
        _fail("bound channel profile target root is outside runtime_root")
    return tuple(relative.parts)


def _require_result_profile_binding(
    result: Mapping[str, object], expected: Mapping[str, object], *, label: str,
) -> None:
    try:
        from src.autoslice.channel_profile import require_profile_binding

        require_profile_binding(result.get("profile_binding"), expected)
        summary = result.get("summary")
        if isinstance(summary, Mapping) and "profile_binding" in summary:
            require_profile_binding(summary["profile_binding"], expected)
    except (TypeError, ValueError) as exc:
        _fail(f"{label} is missing or differs from expected profile binding", exc)


def _source_allowed(relative: Path, *, date: str, candidate_id: str) -> bool:
    parts = relative.parts
    prefix = ("out", date)
    if len(parts) >= 3 and parts[:2] == prefix and parts[2] == candidate_id:
        return True
    return relative == Path("out", date, f"spec_{candidate_id}.json") or (
        len(parts) == 3 and parts[:2] == prefix and parts[2].startswith(candidate_id + ".")
    )


def _private_relative(relative: Path, *, lane: str, candidate_id: str) -> bool:
    parts = relative.parts
    return len(parts) >= 4 and parts[:3] == (".prepared-deliveries", lane, candidate_id)


def _failure_evidence_entries(
    result: Mapping[str, object], *, date: str | None, candidate_id: str,
) -> list[dict[str, object]]:
    """Validate the typed relative evidence inventory on a failed result."""

    evidence = result.get("failure_evidence")
    if evidence is None:
        return []
    if not isinstance(evidence, Mapping):
        _fail("failure_evidence is not an object")
    raw_surfaces = evidence.get("surface_files", [])
    if not isinstance(raw_surfaces, list):
        _fail("failure_evidence.surface_files is not a list")
    if raw_surfaces and date is None:
        _fail("failure_evidence surfaces require date")
    entries: list[dict[str, object]] = []
    seen_paths: set[Path] = set()
    seen_roles: set[str] = set()

    def locator(value: object, *, label: str) -> tuple[str, Path]:
        if not isinstance(value, str) or not value or value.startswith("/"):
            _fail(f"{label} must be a candidate-relative path")
        relative = _safe_relative(value, label=label)
        if any(_FAILURE_FILE_COMPONENT.fullmatch(part) is None for part in relative.parts):
            _fail(f"{label} contains an unsafe component")
        if date is None:
            _fail(f"{label} requires date")
        candidate_relative = Path("out", date, candidate_id, *relative.parts)
        if not _source_allowed(candidate_relative, date=date, candidate_id=candidate_id):
            _fail(f"{label} escapes candidate directory")
        return relative.as_posix(), candidate_relative

    for index, raw in enumerate(raw_surfaces):
        if not isinstance(raw, Mapping) or set(raw) != {"file", "role", "sha256"}:
            _fail(f"failure_evidence.surface_files[{index}] schema is invalid")
        relative_text, candidate_relative = locator(
            raw.get("file"), label=f"failure_evidence.surface_files[{index}].file"
        )
        role = raw.get("role")
        if not isinstance(role, str) or _COMPONENT.fullmatch(role) is None:
            _fail(f"failure_evidence.surface_files[{index}].role is invalid")
        if role in seen_roles:
            _fail("failure_evidence surface roles collide")
        seen_roles.add(role)
        digest = _canonical_hash(
            raw.get("sha256"), label=f"failure_evidence.surface_files[{index}].sha256"
        )
        if candidate_relative in seen_paths:
            _fail("failure_evidence surface paths collide")
        seen_paths.add(candidate_relative)
        entries.append({"file": relative_text, "path": candidate_relative, "role": role, "sha256": digest})

    if "artifact_file" in evidence:
        artifact_text, artifact_relative = locator(
            evidence.get("artifact_file"), label="failure_evidence.artifact_file"
        )
        matched = any(entry["path"] == artifact_relative and entry["file"] == artifact_text
                      for entry in entries)
        # The producer also reports this diagnostic locator when the native
        # audit is missing or unreadable. That known failure shape must remain
        # recoverable without inventing bytes for the missing surface.
        if not matched and evidence.get("audit_loaded") is not False:
            _fail("failure_evidence.artifact_file is absent from surface_files")
    return entries


def _safe_existing(path: Path, *, label: str) -> os.stat_result:
    return _lstat_regular(path, label=label)


def _json_path_dependencies(
    path: Path, *, root: Path, date: str, candidate_id: str,
    excluded: set[Path], source_bindings: Mapping[Path, Mapping[str, object]], add,
) -> None:
    """Follow JSON evidence references while keeping the path allowlist bounded."""

    try:
        payload = _read_regular(path, label=f"dependency {path}")
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OverflowBundleError):
        # A binary/non-JSON evidence file is still copied when its locator was
        # explicit.  It simply cannot contribute another dependency edge.
        return
    if isinstance(document, Mapping) and document.get("schema_version") == "source-subtitle-truth-ledger.v1":
        # Frozen repository authority is transported as an exact opaque input.
        # Its historical review citations are not this candidate's file graph.
        expected_root = root / "out" / date / candidate_id / "source-truth-input"
        if (path.parent != expected_root
                or path.name != "ledger." + hashlib.sha256(payload).hexdigest() + ".json"
                or not isinstance(document.get("entries"), list)):
            _fail("frozen source truth ledger binding drifts")
        return
    for value, _keys in _iter_strings(document):
        if not _path_like(value):
            continue
        candidate = Path(value)
        if _source_binding_key(candidate) in source_bindings:
            continue
        try:
            relative = _relative_to_root(candidate, root, label="JSON dependency")
        except OverflowBundleError:
            _fail(f"JSON dependency escapes runtime_root: {path} at {'/'.join(_keys)}")
        if relative in excluded or not _source_allowed(relative, date=date, candidate_id=candidate_id):
            continue
        try:
            observed = os.lstat(candidate)
        except FileNotFoundError:
            _fail(f"JSON dependency is missing: {candidate}")
        except OSError as exc:
            _fail("JSON dependency is unavailable", exc)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            _fail(f"JSON dependency is not a regular file: {candidate}")
        add(candidate, "source")


def _candidate_identity(
    *, root: Path, result: Mapping[str, object], document: Mapping[str, object] | None,
    date: str | None, candidate_id: str | None,
) -> tuple[str, str | None]:
    cid = candidate_id
    if cid is None and document is not None:
        cid = str(document.get("candidate_id") or "")
    if cid is None:
        cid = str(result.get("candidate_id") or "")
    cid = _identity(cid, label="candidate_id")
    if _COMPONENT.fullmatch(cid) is None:
        _fail("candidate_id is unsafe")
    if date is not None:
        if _DATE.fullmatch(date) is None:
            _fail("date is unsafe")
        return cid, date
    for value, _keys in _iter_strings(result):
        if not _path_like(value):
            continue
        try:
            relative = _relative_to_root(Path(value), root, label="result path")
        except OverflowBundleError:
            continue
        parts = relative.parts
        if len(parts) >= 3 and parts[0] == "out" and (parts[2] == cid or parts[2].startswith(cid + ".")):
            if _DATE.fullmatch(parts[1]):
                return cid, parts[1]
    return cid, None


def _dependency_paths(
    *, root: Path, result: Mapping[str, object], handle: PreparedDelivery | None,
    document: Mapping[str, object] | None, date: str | None, candidate_id: str,
    source_bindings: Mapping[Path, Mapping[str, object]],
    target_root: tuple[str, ...],
) -> tuple[dict[Path, tuple[Path, str]], list[dict[str, object]]]:
    """Return source files and the exact original prepared artifact map."""

    dependencies: dict[Path, tuple[Path, str]] = {}
    excluded_targets: set[Path] = set()
    prepared_entries: list[dict[str, object]] = []

    def add(path: Path, kind: str) -> None:
        relative = _relative_to_root(path, root, label="dependency path")
        if relative in excluded_targets:
            return
        _safe_existing(path, label=f"dependency {path}")
        previous = dependencies.get(relative)
        if previous is None:
            dependencies[relative] = (path, kind)

    if handle is not None and document is not None:
        manifest_relative = _relative_to_root(handle.manifest_path, root, label="prepared manifest")
        if not _private_relative(manifest_relative, lane=handle.lane, candidate_id=handle.candidate_id):
            _fail("prepared manifest is outside its private namespace")
        add(handle.manifest_path, "prepared_manifest")
        artifacts = document.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            _fail("prepared manifest has no artifacts")
        for raw in artifacts:
            if not isinstance(raw, Mapping):
                _fail("prepared artifact entry is invalid")
            role = raw.get("role")
            if not isinstance(role, str) or _COMPONENT.fullmatch(role) is None:
                _fail("prepared artifact role is unsafe")
            source_value = raw.get("source_path")
            staged_value = raw.get("staged_path")
            target_value = raw.get("target_path")
            if not all(isinstance(v, str) for v in (source_value, staged_value, target_value)):
                _fail(f"prepared artifact {role} has invalid paths")
            source = Path(str(source_value))
            staged = Path(str(staged_value))
            target = Path(str(target_value))
            source_relative = _relative_to_root(source, root, label=f"prepared {role} source")
            staged_relative = _relative_to_root(staged, root, label=f"prepared {role} staged")
            target_relative = _relative_to_root(target, root, label=f"prepared {role} target")
            if not _source_allowed(source_relative, date=date or "", candidate_id=candidate_id):
                _fail(f"prepared {role} source is outside bounded out/date allowlist")
            if not _private_relative(staged_relative, lane=handle.lane, candidate_id=handle.candidate_id):
                _fail(f"prepared {role} staged path is outside private namespace")
            target_date_index = target_relative.parts.index(date or "") if date and date in target_relative.parts else -1
            if target_date_index < 1:
                _fail(f"prepared {role} target is outside profile delivery namespace")
            if not _target_allowed(target_relative, date=date or "", target_root=target_root):
                _fail(f"prepared {role} target is outside profile delivery namespace")
            if target_relative in excluded_targets:
                _fail("prepared delivery targets collide")
            excluded_targets.add(target_relative)
            source_hash = _canonical_hash(raw.get("source_sha256"), label=f"prepared {role} source hash")
            staged_hash = _canonical_hash(raw.get("staged_sha256"), label=f"prepared {role} staged hash")
            source_digest, source_bytes = _hash_regular(source, label=f"prepared {role} source")
            staged_digest, staged_bytes = _hash_regular(staged, label=f"prepared {role} staged")
            if source_digest != source_hash:
                _fail(f"prepared {role} source hash drifts")
            if staged_digest != staged_hash:
                _fail(f"prepared {role} staged hash drifts")
            add(source, "source")
            add(staged, "prepared_staged")
            prepared_entries.append({
                "role": role,
                "source_path": source_relative.as_posix(),
                "source_sha256": source_hash,
                "source_bytes": source_bytes,
                "staged_path": staged_relative.as_posix(),
                "staged_sha256": staged_hash,
                "staged_bytes": staged_bytes,
                "target_path": target_relative.as_posix(),
            })

    if date is not None:
        candidate_root = root / "out" / date / candidate_id
        if candidate_root.exists() or candidate_root.is_symlink():
            if candidate_root.is_symlink():
                _fail("candidate root is a symlink")
            # Candidate root is a bounded *namespace*.  Only files named by a
            # result/spec/evidence locator are copied; large unreferenced PCM
            # and recut intermediates remain on the worker.
        date_root = root / "out" / date
        for direct in (date_root / f"spec_{candidate_id}.json",):
            if direct.exists() or direct.is_symlink():
                if direct.is_symlink():
                    _fail("candidate spec is a symlink")

    for value, keys in _iter_strings(result):
        if not _path_like(value):
            continue
        path = Path(value)
        if _source_binding_key(path) in source_bindings:
            # This is an input preimage supplied by the worker request.  Keep
            # the original locator in the cloned result, but bind its bytes
            # in the manifest instead of copying it into the bundle.
            continue
        try:
            relative = _relative_to_root(path, root, label="result path")
        except OverflowBundleError:
            # An absolute locator outside the shared logical root cannot be
            # made valid on the controller.  Failing closed avoids exporting
            # a result that silently points back to worker-only bytes.
            _fail(f"result dependency escapes runtime_root: {value}")
        if relative in excluded_targets:
            continue
        if date is None:
            continue
        if not _source_allowed(relative, date=date, candidate_id=candidate_id):
            # State/queue/log/repo paths are intentionally excluded.  An
            # absolute path in any other runtime namespace is ambiguous and
            # must fail closed instead of silently producing an incomplete
            # dependency closure.
            if relative.parts and relative.parts[0] in {
                "state", "queue", "queues", "logs", "reports", "repo",
                "sourcecache", "credentials", "runtime-scratch", ".delivery-journal",
                ".producer-batch-journal", ".prepared-deliveries",
            }:
                continue
            _fail(f"result dependency is outside bounded out/date allowlist: {value}")
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            # Delivery targets are commonly absent during prepare.  A missing
            # source locator is an error; this check is deliberately after the
            # target exclusion above.
            _fail(f"result dependency is missing: {path}")
        except OSError as exc:
            _fail("result dependency is unavailable", exc)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            _fail(f"result dependency is not a regular file: {path}")
        add(path, "source")

    # Failed producer results carry a small typed inventory whose locators are
    # candidate-relative rather than absolute.  Resolve only those exact
    # files; never scan the candidate directory or infer names from prose.
    for entry in _failure_evidence_entries(result, date=date, candidate_id=candidate_id):
        path = root / entry["path"]
        observed_hash, _observed_bytes = _hash_regular(
            path, label=f"failure evidence {entry['file']}"
        )
        if observed_hash != entry["sha256"]:
            _fail(f"failure evidence hash drifts: {entry['file']}")
        add(path, "source")

    # Follow nested JSON evidence after the first closure pass.  A referenced
    # binary is retained unchanged, while a referenced JSON may add another
    # bounded source locator.
    queue = list(dependencies.values())
    seen_json: set[Path] = set()
    while queue:
        path, kind = queue.pop()
        if path in seen_json or path.suffix.lower() not in {".json", ".jsn"}:
            continue
        seen_json.add(path)
        before = set(dependencies)
        _json_path_dependencies(
            path, root=root, date=date or "", candidate_id=candidate_id,
            excluded=excluded_targets, source_bindings=source_bindings, add=add,
        )
        queue.extend(value for key, value in dependencies.items() if key not in before)

    return dependencies, prepared_entries


def _file_entry(
    *, root: Path, path: Path, kind: str, bundle_root: Path, bundle_payload: str,
) -> dict[str, object]:
    relative = _relative_to_root(path, root, label="bundle source")
    payload_path = bundle_root / _safe_relative(bundle_payload, label="bundle payload")
    digest, size = _copy_regular_create_only(
        path, payload_path, root=bundle_root, label=f"bundle payload {relative.as_posix()}",
    )
    return {
        "path": relative.as_posix(),
        "payload": _safe_relative(bundle_payload, label="bundle payload").as_posix(),
        "kind": kind,
        "bytes": size,
        "sha256": digest,
    }


def _manifest_digest(document: Mapping[str, object]) -> str:
    body = dict(document)
    body.pop("manifest_sha256", None)
    return _sha256_bytes(_canonical_bytes(body))


def _bundle_root_for_write(bundle_dir: Path) -> Path:
    bundle = Path(bundle_dir)
    if not bundle.is_absolute():
        bundle = bundle.absolute()
    if bundle.exists() or bundle.is_symlink():
        if bundle.is_symlink() or not bundle.is_dir():
            _fail("bundle_dir is not a directory")
        try:
            if any(bundle.iterdir()):
                _fail("bundle_dir is not empty")
        except OSError as exc:
            _fail("bundle_dir is unavailable", exc)
    else:
        try:
            bundle.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            _fail("cannot create bundle_dir", exc)
    return bundle


def export_bundle(
    result: Mapping[str, object],
    runtime_root: Path,
    *,
    bundle_dir: Path,
    job_id: str,
    attempt_id: str,
    release_sha256: str,
    input_sha256: str,
    source_files: list[dict[str, object]] | None = None,
    date: str | None = None,
    lane: str | None = None,
    expected_profile_binding: Mapping[str, object] | None = None,
) -> Path:
    """Export one bounded producer result and return ``bundle.json``.

    A successful ``delivery_prepared_no_target`` result must carry a valid
    Talk/Song handle.  Failed or rejected results are exported as evidence
    only; they never receive a fabricated prepared handle.
    """

    root = _safe_runtime_root(Path(runtime_root))
    if not isinstance(result, Mapping):
        _fail("producer result is not an object")
    result_copy = _json_object(result, label="producer result")
    bound_profile, profile_binding = _bound_profile(root, expected_profile_binding)
    _require_result_profile_binding(
        result_copy, profile_binding, label="producer result profile_binding"
    )
    job = _identity(job_id, label="job_id")
    attempt = _identity(attempt_id, label="attempt_id")
    release = _canonical_hash(release_sha256, label="release_sha256")
    input_digest = _canonical_hash(input_sha256, label="input_sha256")
    _identity_matches_result(result_copy, job_id=job, attempt_id=attempt, release=release, input_sha=input_digest)
    source_bindings, normalized_source_files = _source_file_bindings(
        root, source_files, label="source_files"
    )
    if date is not None and _DATE.fullmatch(date) is None:
        _fail("date is unsafe")
    if lane is not None and _COMPONENT.fullmatch(lane) is None:
        _fail("lane is unsafe")

    handle, prepared_document = _prepared_context(
        runtime_root=root, result=result_copy, status=result_copy.get("status"),
    )
    if handle is not None:
        if lane is not None and lane != handle.lane:
            _fail("lane differs from prepared handle")
        lane = handle.lane
        if prepared_document is not None and date is None:
            _candidate, date = _candidate_identity(
                root=root, result=result_copy, document=prepared_document,
                date=None, candidate_id=handle.candidate_id,
            )
    candidate_id, date = _candidate_identity(
        root=root, result=result_copy, document=prepared_document,
        date=date, candidate_id=handle.candidate_id if handle else None,
    )
    if prepared_document is not None and str(prepared_document.get("candidate_id")) != candidate_id:
        _fail("prepared candidate identity differs from result")
    if handle is not None and date is None:
        _fail("prepared result has no date")

    configured_target_root = _bound_profile_target_root(root, bound_profile)
    dependencies, prepared_entries = _dependency_paths(
        root=root, result=result_copy, handle=handle, document=prepared_document,
        date=date, candidate_id=candidate_id, source_bindings=source_bindings,
        target_root=configured_target_root,
    )
    for prepared_entry in prepared_entries:
        target_relative = _safe_relative(
            prepared_entry["target_path"], label="prepared target path"
        )
        if date is None or date not in target_relative.parts:
            _fail("prepared target is outside configured channel profile")
        target_date_index = target_relative.parts.index(date)
        if target_relative.parts[:target_date_index] != configured_target_root:
            _fail("prepared target differs from deployed channel profile")
    requested_bundle = Path(bundle_dir)
    if not requested_bundle.is_absolute():
        requested_bundle = requested_bundle.absolute()
    try:
        if requested_bundle.is_relative_to(root):
            _fail("bundle_dir may not be inside runtime_root")
    except ValueError:
        pass
    bundle = _bundle_root_for_write(requested_bundle)
    files: list[dict[str, object]] = []
    total_bytes = 0
    for relative in sorted(dependencies, key=lambda item: item.as_posix()):
        path, kind = dependencies[relative]
        payload_relative = Path("payload") / relative
        entry = _file_entry(
            root=root, path=path, kind=kind,
            bundle_root=bundle,
            bundle_payload=payload_relative.as_posix(),
        )
        files.append(entry)
        total_bytes += int(entry["bytes"])

    manifest_relative = None
    prepared_payload = None
    if handle is not None and prepared_document is not None:
        manifest_relative = _relative_to_root(handle.manifest_path, root, label="prepared manifest")
        prepared_payload = {
            "lane": handle.lane,
            "candidate_id": handle.candidate_id,
            "manifest_path": manifest_relative.as_posix(),
            "prepared_sha256": f"sha256:{handle.prepared_sha256}",
            "artifacts": prepared_entries,
        }
    document: dict[str, object] = {
        "schema_version": OVERFLOW_BUNDLE_SCHEMA,
        "job_id": job,
        "attempt_id": attempt,
        "release_sha256": release,
        "input_sha256": input_digest,
        "runtime_root": str(root),
        "date": date,
        "lane": lane,
        "candidate_id": candidate_id,
        "profile_binding": profile_binding,
        "result": result_copy,
        "source_files": normalized_source_files,
        "prepared": prepared_payload,
        "files": files,
        "payload_files": len(files),
        "payload_bytes": total_bytes,
    }
    document["manifest_sha256"] = _manifest_digest(document)
    bundle_manifest = bundle / BUNDLE_FILENAME
    _create_only(
        bundle_manifest, _canonical_bytes(document), root=bundle,
        label="bundle manifest",
    )
    return bundle_manifest


def _read_bundle_manifest(bundle_dir: Path) -> tuple[Path, dict[str, Any]]:
    bundle = Path(bundle_dir)
    if not bundle.is_absolute():
        bundle = bundle.absolute()
    try:
        root_stat = os.lstat(bundle)
    except OSError as exc:
        _fail("bundle_dir is unavailable", exc)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _fail("bundle_dir is unsafe")
    path = bundle / BUNDLE_FILENAME
    try:
        payload = _read_regular(path, label="bundle manifest")
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OverflowBundleError) as exc:
        _fail("bundle manifest is invalid", exc)
    if isinstance(document, dict) and document.get("schema_version") == "autoslice-overflow-bundle.v1":
        _fail("legacy overflow bundle v1 is unsupported; explicit profile binding is required")
    if not isinstance(document, dict) or document.get("schema_version") != OVERFLOW_BUNDLE_SCHEMA:
        _fail("bundle schema drifts")
    expected = document.get("manifest_sha256")
    if not isinstance(expected, str) or expected != _manifest_digest(document):
        _fail("bundle manifest digest drifts")
    return bundle, document


def _validate_bundle_files(bundle: Path, document: Mapping[str, object], runtime_root: Path) -> dict[Path, Path]:
    raw_files = document.get("files")
    if not isinstance(raw_files, list):
        _fail("bundle files inventory is invalid")
    expected_payloads: set[Path] = set()
    payloads: dict[Path, Path] = {}
    listed_paths: set[Path] = set()
    total = 0
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            _fail("bundle file entry is invalid")
        relative = _safe_relative(raw.get("path"), label="bundle file path")
        payload_relative = _safe_relative(raw.get("payload"), label="bundle payload path")
        if relative in listed_paths:
            _fail("bundle file paths collide")
        listed_paths.add(relative)
        if payload_relative in expected_payloads:
            _fail("bundle payload paths collide")
        expected_payloads.add(payload_relative)
        if payload_relative.parts[0] != "payload":
            _fail("bundle payload escapes payload namespace")
        path = bundle / payload_relative
        digest, size = _hash_regular(path, label=f"bundle payload {relative.as_posix()}")
        if raw.get("bytes") != size:
            _fail(f"bundle byte count drifts: {relative.as_posix()}")
        if raw.get("sha256") != digest:
            _fail(f"bundle file hash drifts: {relative.as_posix()}")
        payloads[relative] = path
        total += size
        if raw.get("kind") not in {"source", "prepared_manifest", "prepared_staged"}:
            _fail(f"bundle file kind is invalid: {relative.as_posix()}")
    if document.get("payload_files") != len(raw_files) or document.get("payload_bytes") != total:
        _fail("bundle payload inventory summary drifts")

    # Unknown payload files would evade the manifest's exact ownership.
    payload_root = bundle / "payload"
    try:
        for base, directories, names in os.walk(payload_root, followlinks=False):
            base_path = Path(base)
            for name in directories:
                child = base_path / name
                if child.is_symlink():
                    _fail("bundle payload contains a symlink directory")
            for name in names:
                child = base_path / name
                if child.is_symlink():
                    _fail("bundle payload contains a symlink file")
                relative = child.relative_to(bundle)
                if relative not in expected_payloads:
                    _fail("bundle contains an unlisted payload file")
    except OSError as exc:
        _fail("bundle payload inventory cannot be inspected", exc)
    return payloads


def _validate_context(
    document: Mapping[str, object], *, runtime_root: Path, job_id: str,
    attempt_id: str, release_sha256: str, input_sha256: str,
) -> tuple[dict[str, Any], str, str | None, str | None]:
    if document.get("job_id") != _identity(job_id, label="expected_job_id"):
        _fail("bundle job_id differs from expected job")
    if document.get("attempt_id") != _identity(attempt_id, label="expected_attempt_id"):
        _fail("bundle attempt_id differs from expected attempt")
    expected_release = _canonical_hash(release_sha256, label="expected_release_sha256")
    expected_input = _canonical_hash(input_sha256, label="expected_input_sha256")
    if document.get("release_sha256") != expected_release:
        _fail("bundle release_sha256 differs from expected release")
    if document.get("input_sha256") != expected_input:
        _fail("bundle input_sha256 differs from expected input")
    root_string = document.get("runtime_root")
    if not isinstance(root_string, str) or root_string != str(runtime_root):
        _fail("bundle runtime_root differs from controller runtime_root")
    result = _json_object(document.get("result"), label="bundle result")
    _identity_matches_result(result, job_id=str(job_id), attempt_id=str(attempt_id), release=expected_release, input_sha=expected_input)
    date = document.get("date")
    if date is not None and (not isinstance(date, str) or _DATE.fullmatch(date) is None):
        _fail("bundle date is unsafe")
    lane = document.get("lane")
    if lane is not None and (not isinstance(lane, str) or _COMPONENT.fullmatch(lane) is None):
        _fail("bundle lane is unsafe")
    candidate_id = document.get("candidate_id")
    if not isinstance(candidate_id, str) or _COMPONENT.fullmatch(candidate_id) is None:
        _fail("bundle candidate_id is unsafe")
    return result, candidate_id, date, lane


def _validate_prepared_metadata(
    document: Mapping[str, object], *, payloads: Mapping[Path, Path],
    runtime_root: Path, date: str | None, candidate_id: str, lane: str | None,
    target_root: tuple[str, ...],
) -> tuple[dict[str, Any] | None, list[DeliveryArtifact]]:
    raw_prepared = document.get("prepared")
    result = document.get("result")
    status = result.get("status") if isinstance(result, Mapping) else None
    if raw_prepared is None:
        if status == "delivery_prepared_no_target":
            _fail("successful prepare bundle lacks prepared metadata")
        return None, []
    if not isinstance(raw_prepared, Mapping):
        _fail("bundle prepared metadata is invalid")
    if set(raw_prepared) != {
        "lane", "candidate_id", "manifest_path", "prepared_sha256", "artifacts"
    }:
        _fail("bundle prepared metadata schema drifts")
    if status != "delivery_prepared_no_target":
        _fail("non-prepared result carries a prepared handle")
    prepared_lane = raw_prepared.get("lane")
    prepared_candidate = raw_prepared.get("candidate_id")
    if not isinstance(prepared_lane, str) or prepared_lane != lane:
        _fail("prepared lane differs from bundle lane")
    if not isinstance(prepared_candidate, str) or prepared_candidate != candidate_id:
        _fail("prepared candidate differs from bundle candidate")
    manifest_rel = _safe_relative(raw_prepared.get("manifest_path"), label="prepared manifest path")
    if not _private_relative(manifest_rel, lane=prepared_lane, candidate_id=candidate_id):
        _fail("prepared manifest path escapes private namespace")
    manifest_path = payloads.get(manifest_rel)
    if manifest_path is None:
        _fail("prepared manifest payload is missing")
    if raw_prepared.get("prepared_sha256") is None:
        _fail("prepared digest is missing")
    expected_prepared = _canonical_hash(raw_prepared.get("prepared_sha256"), label="prepared digest")
    try:
        prepared_document = json.loads(_read_regular(manifest_path, label="prepared manifest payload").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("prepared manifest payload is invalid", exc)
    if not isinstance(prepared_document, dict):
        _fail("prepared manifest payload is not an object")
    if prepared_document.get("schema_version") != "producer-prepared-delivery.v1":
        _fail("prepared manifest schema is invalid")
    if prepared_document.get("lane") != prepared_lane or prepared_document.get("candidate_id") != candidate_id:
        _fail("prepared manifest identity drifts")
    unsigned = dict(prepared_document)
    prepared_digest = unsigned.pop("prepared_sha256", None)
    if prepared_digest != expected_prepared or _sha256_bytes(_canonical_bytes(unsigned)) != expected_prepared:
        _fail("prepared manifest digest drifts")
    artifacts = prepared_document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        _fail("prepared manifest artifacts are invalid")
    delivery_artifacts: list[DeliveryArtifact] = []
    metadata_artifacts: list[dict[str, object]] = []
    roles: set[str] = set()
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            _fail("prepared artifact entry is invalid")
        role = raw.get("role")
        if not isinstance(role, str) or _COMPONENT.fullmatch(role) is None or role in roles:
            _fail("prepared artifact role is invalid or duplicated")
        roles.add(role)
        source_rel = _safe_relative(_relative_from_absolute(raw.get("source_path"), runtime_root, label=f"prepared {role} source"), label=f"prepared {role} source")
        staged_rel = _safe_relative(_relative_from_absolute(raw.get("staged_path"), runtime_root, label=f"prepared {role} staged"), label=f"prepared {role} staged")
        target_rel = _safe_relative(_relative_from_absolute(raw.get("target_path"), runtime_root, label=f"prepared {role} target"), label=f"prepared {role} target")
        if not _source_allowed(source_rel, date=date or "", candidate_id=candidate_id):
            _fail(f"prepared {role} source escapes allowlist")
        if not _private_relative(staged_rel, lane=prepared_lane, candidate_id=candidate_id):
            _fail(f"prepared {role} staged path escapes private namespace")
        target_date_index = target_rel.parts.index(date or "") if date and date in target_rel.parts else -1
        if target_date_index < 1:
            _fail(f"prepared {role} target escapes delivery namespace")
        if not _target_allowed(target_rel, date=date or "", target_root=target_root):
            _fail(f"prepared {role} target escapes delivery namespace")
        source_hash = _canonical_hash(raw.get("source_sha256"), label=f"prepared {role} source hash")
        staged_hash = _canonical_hash(raw.get("staged_sha256"), label=f"prepared {role} staged hash")
        source_payload = payloads.get(source_rel)
        staged_payload = payloads.get(staged_rel)
        source_size = raw.get("source_size")
        staged_size = raw.get("staged_size")
        source_digest, source_bytes = _hash_regular(source_payload, label=f"prepared {role} source payload") if source_payload is not None else (None, None)
        staged_digest, staged_bytes = _hash_regular(staged_payload, label=f"prepared {role} staged payload") if staged_payload is not None else (None, None)
        if source_payload is None or source_digest != source_hash or source_size != source_bytes:
            _fail(f"prepared {role} source payload drifts")
        if staged_payload is None or staged_digest != staged_hash or staged_size != staged_bytes:
            _fail(f"prepared {role} staged payload drifts")
        target_path = runtime_root / target_rel
        try:
            observed = os.lstat(target_path)
        except FileNotFoundError:
            observed = None
        except OSError as exc:
            _fail("delivery target is unavailable", exc)
        if observed is not None:
            _fail(f"delivery target already exists: {target_rel.as_posix()}")
        source_path = runtime_root / source_rel
        metadata_artifacts.append({
            "role": role,
            "source_path": source_rel.as_posix(),
            "source_sha256": source_hash,
            "source_bytes": source_bytes,
            "staged_path": staged_rel.as_posix(),
            "staged_sha256": staged_hash,
            "staged_bytes": staged_bytes,
            "target_path": target_rel.as_posix(),
        })
        delivery_artifacts.append(DeliveryArtifact(role, source_path, target_path, source_hash))
    if raw_prepared.get("artifacts") != metadata_artifacts:
        _fail("bundle prepared artifact metadata drifts")
    return dict(raw_prepared), delivery_artifacts


def _relative_from_absolute(value: object, root: Path, *, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} is invalid")
    relative = _relative_to_root(Path(value), root, label=label)
    return relative.as_posix()


def _replace_handle(result: dict[str, Any], handle: PreparedDelivery) -> dict[str, Any]:
    updated = copy.deepcopy(result)
    reference = _prepared_reference(updated)
    if reference is None:
        _fail("prepared result has no replaceable handle")
    replacement = {
        "manifest_path": str(handle.manifest_path),
        "prepared_sha256": f"sha256:{handle.prepared_sha256}",
        "upload_enabled": False,
    }
    if isinstance(updated.get("prepared_delivery"), Mapping):
        original = dict(updated["prepared_delivery"])
        original.update(replacement)
        updated["prepared_delivery"] = original
        return updated
    summary = updated.get("prepared_summary")
    if isinstance(summary, Mapping) and isinstance(summary.get("prepared_delivery"), Mapping):
        summary_copy = dict(summary)
        original = dict(summary_copy["prepared_delivery"])
        original.update(replacement)
        summary_copy["prepared_delivery"] = original
        updated["prepared_summary"] = summary_copy
        return updated
    _fail("prepared result has no replaceable handle")


def import_bundle(
    bundle_dir: Path,
    runtime_root: Path,
    expected_job_id: str,
    expected_attempt_id: str,
    expected_release_sha256: str,
    expected_input_sha256: str,
    expected_profile_binding: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Import a bundle and return the original producer result projection.

    Candidate source/evidence paths are create-only.  Existing equal bytes are
    accepted; conflicting files, symlinks, targets, state, and repo payloads
    are rejected.  A successful prepared result receives a newly prepared
    handle whose inode/device observations belong to the controller runtime.
    """

    root = _safe_runtime_root(Path(runtime_root))
    bundle, document = _read_bundle_manifest(Path(bundle_dir))
    bound_profile, profile_binding = _bound_profile(root, expected_profile_binding)
    _require_result_profile_binding(
        document, profile_binding, label="bundle profile_binding"
    )
    result, candidate_id, date, lane = _validate_context(
        document, runtime_root=root, job_id=expected_job_id,
        attempt_id=expected_attempt_id, release_sha256=expected_release_sha256,
        input_sha256=expected_input_sha256,
    )
    _require_result_profile_binding(
        result, profile_binding, label="bundle result profile_binding"
    )
    # Validate worker-declared external input preimages before inspecting or
    # materializing any candidate payload.  These paths are provenance only;
    # their original result locators remain unchanged.
    _source_file_bindings(
        root, document.get("source_files", []), label="bundle source_files"
    )
    payloads = _validate_bundle_files(bundle, document, root)
    raw_files = document.get("files")
    assert isinstance(raw_files, list)
    file_entries = {
        _safe_relative(entry["path"], label="bundle file path"): entry
        for entry in raw_files if isinstance(entry, Mapping)
    }
    kinds = {
        _safe_relative(entry["path"], label="bundle file path"): entry.get("kind")
        for entry in raw_files if isinstance(entry, Mapping)
    }
    for evidence_entry in _failure_evidence_entries(
        result, date=date, candidate_id=candidate_id
    ):
        relative = evidence_entry["path"]
        listed = file_entries.get(relative)
        if not isinstance(listed, Mapping) or listed.get("kind") != "source":
            _fail(f"failure evidence is absent from bundle files: {evidence_entry['file']}")
        listed_hash = _canonical_hash(
            listed.get("sha256"), label=f"failure evidence {evidence_entry['file']} bundle hash"
        )
        if listed_hash != evidence_entry["sha256"]:
            _fail(f"failure evidence bundle hash drifts: {evidence_entry['file']}")
    configured_target_root = _bound_profile_target_root(root, bound_profile)
    prepared_metadata, artifacts = _validate_prepared_metadata(
        document, payloads=payloads, runtime_root=root,
        date=date, candidate_id=candidate_id, lane=lane,
        target_root=configured_target_root,
    )
    if prepared_metadata is not None:
        target_roots = {
            tuple(Path(artifact.target).relative_to(root).parts[:Path(artifact.target).relative_to(root).parts.index(date)])
            for artifact in artifacts
            if date is not None and date in Path(artifact.target).relative_to(root).parts
        }
        if target_roots and target_roots != {configured_target_root}:
            _fail("prepared target root differs from deployed channel profile")

    # Older prepared retries exported one cumulative producer.log. The
    # validated received payload already preserves that exact attempt's
    # observed bytes. Read this diagnostic from its job/attempt bundle rather
    # than overwriting a different attempt's controller-local log. This does
    # not relax create-only handling of any producer input or result artifact.
    retained_log = None
    raw_log = result.get("log")
    if (prepared_metadata is None and result.get("status") == "failed"
            and isinstance(raw_log, str) and Path(raw_log).is_absolute()):
        log_relative = _relative_to_root(Path(raw_log), root, label="producer log")
        parts = log_relative.parts
        if (len(parts) == 6 and parts[:4] == ("out", date, candidate_id, ".prepared-resumes")
                and re.fullmatch(r"[0-9a-f]{64}", parts[4])
                and parts[5] == "producer.log" and kinds.get(log_relative) == "source"):
            retained_log = log_relative

    # Materialize only source/evidence files.  The original prepared.json and
    # staged bytes remain bundle-local proof; writing them into runtime would
    # preserve stale inode/device claims and recreate the original bug.
    for relative, payload_path in payloads.items():
        kind = kinds.get(relative)
        if kind in {"prepared_manifest", "prepared_staged"}:
            continue
        if relative == retained_log:
            continue
        if relative.parts and relative.parts[0] == "repo":
            _fail("bundle may not materialize repo files")
        if date is None or not _source_allowed(relative, date=date, candidate_id=candidate_id):
            _fail(f"bundle source path is outside bounded allowlist: {relative.as_posix()}")
        entry = file_entries[relative]
        expected_sha = entry.get("sha256")
        expected_bytes = entry.get("bytes")
        if not isinstance(expected_sha, str) or isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int):
            _fail(f"bundle source metadata is invalid: {relative.as_posix()}")
        _copy_regular_create_only(
            payload_path, root / relative, root=root,
            label=f"candidate source {relative.as_posix()}",
            expected_sha256=expected_sha, expected_bytes=expected_bytes,
        )

    if prepared_metadata is None:
        if retained_log is not None:
            result = {**result, "log": str(payloads[retained_log]),
                "imported_producer_log": {
                    "schema_version": "overflow-received-producer-log.v1",
                    "original_locator": raw_log,
                    "sha256": file_entries[retained_log]["sha256"],
                    "bytes": file_entries[retained_log]["bytes"],
                    "job_id": expected_job_id, "attempt_id": expected_attempt_id,
                    "bundle_manifest_sha256": document["manifest_sha256"],
                }}
        return result

    try:
        current_authority = deployment_authority_binding(root)
    except ProducerDeliveryTransactionError as exc:
        _fail("controller deployment authority is invalid", exc)
    manifest_payload = payloads[_safe_relative(prepared_metadata["manifest_path"], label="prepared manifest path")]
    prepared_document = json.loads(_read_regular(manifest_payload, label="prepared manifest payload").decode("utf-8"))
    if prepared_document.get("deployed_authority") != current_authority:
        _fail("prepared deployment authority differs from controller")
    try:
        from src.autoslice.channel_profile import binding_for_profile, require_profile_binding
        require_profile_binding(binding_for_profile(bound_profile), profile_binding)
        fresh = prepare_delivery(
            runtime_root=root,
            lane=str(prepared_metadata["lane"]),
            candidate_id=candidate_id,
            artifacts=artifacts,
            deployed_authority=current_authority,
        )
    except (ProducerDeliveryTransactionError, OSError, ValueError) as exc:
        _fail("fresh controller prepare failed", exc)
    return _replace_handle(result, fresh)


# Explicit names for callers that prefer the longer description.  Keep the
# root-facing API above stable because the worker/controller adapters call it.
export_prepared_bundle = export_bundle
import_prepared_bundle = import_bundle
export_overflow_bundle = export_bundle
import_overflow_bundle = import_bundle


__all__ = [
    "BUNDLE_FILENAME",
    "OVERFLOW_BUNDLE_SCHEMA",
    "OverflowBundleError",
    "ProducerOverflowBundleError",
    "export_bundle",
    "import_bundle",
    "export_prepared_bundle",
    "import_prepared_bundle",
    "export_overflow_bundle",
    "import_overflow_bundle",
]
