"""Package-contained binding for HOST_ONLY v4 final-pixel evidence.

A v4 receipt is only meaningful when the review package independently binds the
receipt document and every image whose digest it declares.  This module keeps
that check separate from producer-side route validation so a package auditor
never has to trust producer paths or a prior in-memory comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.cover_host_identity_gate import (
    HOST_ONLY_AUTHORITY,
    HOST_ONLY_SCHEMA_VERSION,
)

BINDING_ITEM_KEY = "host_only_v4_binding"
BINDING_SCHEMA_VERSION = "lidousha-host-only-v4-package-binding.v1"
BINDING_ISSUE_CODE = "COVER_HOST_ONLY_V4_PACKAGE_BINDING_MISSING_OR_INVALID"


class HostOnlyV4PackageBindingError(ValueError):
    """The package cannot prove its v4 receipt against contained bytes."""


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalized_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise HostOnlyV4PackageBindingError(f"{label} sha256 is absent")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise HostOnlyV4PackageBindingError(f"{label} sha256 is malformed")
    return "sha256:" + digest


def _canonical_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise HostOnlyV4PackageBindingError(f"{label} path is absent")
    raw = Path(value)
    if raw.is_absolute() or not raw.parts or any(
        part in {"", ".", ".."} for part in raw.parts
    ):
        raise HostOnlyV4PackageBindingError(f"{label} path is not package-relative")
    if raw.as_posix() != value:
        raise HostOnlyV4PackageBindingError(f"{label} path is not canonical")
    return raw


def _open_directory_chain(path: Path, *, label: str) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute():
        raise HostOnlyV4PackageBindingError(f"{label} path is not absolute")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute.anchor, directory_flags)
    except OSError as exc:
        raise HostOnlyV4PackageBindingError(
            f"{label} is unavailable or contains a symlink component"
        ) from exc
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(
                part,
                directory_flags | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise HostOnlyV4PackageBindingError(f"{label} is not a directory")
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise HostOnlyV4PackageBindingError(
            f"{label} is unavailable or contains a symlink component"
        ) from exc
    except Exception:
        os.close(descriptor)
        raise


def _read_from_directory(
    directory_descriptor: int,
    relative: Path,
    *,
    label: str,
) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(directory_descriptor)
    try:
        for part in relative.parts[:-1]:
            next_descriptor = os.open(
                part,
                directory_flags | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=descriptor,
        )
        try:
            info = os.fstat(file_descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise HostOnlyV4PackageBindingError(
                    f"{label} is not a regular file"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)
    except OSError as exc:
        raise HostOnlyV4PackageBindingError(
            f"{label} is unavailable or contains a symlink component"
        ) from exc
    finally:
        os.close(descriptor)


def read_regular_file_once(path: Path, *, label: str) -> bytes:
    """Read one external regular file through no-follow directory descriptors."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    parent_descriptor = _open_directory_chain(absolute.parent, label=f"{label} parent")
    try:
        return _read_from_directory(
            parent_descriptor,
            Path(absolute.name),
            label=label,
        )
    finally:
        os.close(parent_descriptor)


def read_package_file_once(
    root: Path,
    locator: object,
    *,
    label: str,
) -> tuple[Path, bytes]:
    """Read one canonical package-relative regular file without following links."""

    relative = _canonical_relative(locator, label=label)
    root_descriptor = _open_directory_chain(root, label="package root")
    try:
        payload = _read_from_directory(root_descriptor, relative, label=label)
    finally:
        os.close(root_descriptor)
    return relative, payload


def parse_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostOnlyV4PackageBindingError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise HostOnlyV4PackageBindingError(f"{label} is not a JSON object")
    return value


def build_binding(
    *,
    candidate_id: str,
    receipt_path: str,
    receipt_bytes: bytes,
    comparison_path: str,
    comparison_bytes: bytes,
    reference_path: str,
    reference_bytes: bytes,
    final_cover_path: str,
    final_cover_bytes: bytes,
) -> dict[str, object]:
    """Return the exact manifest item binding consumed by the auditor."""

    for value, label in (
        (receipt_path, "receipt"),
        (comparison_path, "comparison"),
        (reference_path, "reference"),
        (final_cover_path, "final cover"),
    ):
        _canonical_relative(value, label=label)
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "receipt_path": receipt_path,
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "comparison_path": comparison_path,
        "comparison_sha256": sha256_bytes(comparison_bytes),
        "reference_path": reference_path,
        "reference_sha256": sha256_bytes(reference_bytes),
        "final_cover_path": final_cover_path,
        "final_cover_sha256": sha256_bytes(final_cover_bytes),
        "receipt_schema_version": HOST_ONLY_SCHEMA_VERSION,
        "receipt_authority": HOST_ONLY_AUTHORITY,
    }


def validate_package_binding(
    *,
    root: Path,
    item: Mapping[str, object],
    generation: Mapping[str, object],
) -> None:
    """Recompute every v4 package surface and raise on the first drift."""

    verification = generation.get("final_host_identity_verification")
    if not isinstance(verification, Mapping):
        raise HostOnlyV4PackageBindingError("v4 verification is absent")
    if verification.get("schema_version") != HOST_ONLY_SCHEMA_VERSION:
        raise HostOnlyV4PackageBindingError("verification is not HOST_ONLY v4")
    if verification.get("authority") != HOST_ONLY_AUTHORITY:
        raise HostOnlyV4PackageBindingError("v4 verification authority is invalid")

    binding = item.get(BINDING_ITEM_KEY)
    if not isinstance(binding, Mapping):
        raise HostOnlyV4PackageBindingError("manifest item lacks HOST_ONLY v4 binding")
    if binding.get("schema_version") != BINDING_SCHEMA_VERSION:
        raise HostOnlyV4PackageBindingError("HOST_ONLY v4 binding schema is invalid")
    candidate_id = item.get("candidate_id") or item.get("id")
    if not isinstance(candidate_id, str) or binding.get("candidate_id") != candidate_id:
        raise HostOnlyV4PackageBindingError("HOST_ONLY v4 candidate binding drifts")
    if binding.get("receipt_schema_version") != HOST_ONLY_SCHEMA_VERSION:
        raise HostOnlyV4PackageBindingError("bound receipt schema drifts")
    if binding.get("receipt_authority") != HOST_ONLY_AUTHORITY:
        raise HostOnlyV4PackageBindingError("bound receipt authority drifts")

    receipt_rel, receipt_bytes = read_package_file_once(
        root, binding.get("receipt_path"), label="HOST_ONLY v4 receipt"
    )
    comparison_rel, comparison_bytes = read_package_file_once(
        root, binding.get("comparison_path"), label="HOST_ONLY v4 comparison"
    )
    reference_rel, reference_bytes = read_package_file_once(
        root, binding.get("reference_path"), label="HOST_ONLY v4 reference"
    )
    final_rel, final_bytes = read_package_file_once(
        root, binding.get("final_cover_path"), label="HOST_ONLY v4 final cover"
    )
    if final_rel.as_posix() != item.get("cover"):
        raise HostOnlyV4PackageBindingError("bound final cover is not manifest item.cover")

    actual = {
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "comparison_sha256": sha256_bytes(comparison_bytes),
        "reference_sha256": sha256_bytes(reference_bytes),
        "final_cover_sha256": sha256_bytes(final_bytes),
    }
    for key, value in actual.items():
        if _normalized_sha256(binding.get(key), label=key) != value:
            raise HostOnlyV4PackageBindingError(f"{key} does not match package bytes")

    receipt = parse_json_object(receipt_bytes, label="HOST_ONLY v4 receipt")
    if receipt != dict(verification):
        raise HostOnlyV4PackageBindingError(
            "canonical receipt differs from cover_generation verification"
        )
    if (
        receipt.get("status") != "PASS"
        or receipt.get("host_only_required") is not True
    ):
        raise HostOnlyV4PackageBindingError("canonical receipt is not a HOST_ONLY PASS")
    declared = {
        "comparison_sha256": receipt.get("comparison_sha256"),
        "reference_sha256": receipt.get("reference_sha256"),
        "final_cover_sha256": receipt.get("final_cover_sha256"),
    }
    for key, value in declared.items():
        if _normalized_sha256(value, label=f"receipt {key}") != actual[key]:
            raise HostOnlyV4PackageBindingError(
                f"receipt {key} does not match package bytes"
            )
    witness = receipt.get("witness")
    if not isinstance(witness, Mapping):
        raise HostOnlyV4PackageBindingError("canonical receipt witness is absent")
    witness_sha = _normalized_sha256(
        witness.get("image_sha256"), label="witness image"
    )
    if witness_sha != actual["comparison_sha256"]:
        raise HostOnlyV4PackageBindingError(
            "witness image sha256 does not match package comparison bytes"
        )
    # Keep these variables live as explicit evidence that all four path fields
    # were resolved through the package root rather than producer paths.
    if len({receipt_rel, comparison_rel, reference_rel, final_rel}) != 4:
        raise HostOnlyV4PackageBindingError("HOST_ONLY v4 bound artifacts alias")


def _package_payload_by_hash(
    root: Path,
    *,
    expected_sha256: str,
) -> bytes | None:
    root_descriptor = _open_directory_chain(root, label="package root")
    os.close(root_descriptor)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            _relative, payload = read_package_file_once(
                root,
                relative,
                label="package image candidate",
            )
        except HostOnlyV4PackageBindingError:
            continue
        if sha256_bytes(payload) == expected_sha256:
            return payload
    return None


def _declared_artifact_bytes(
    root: Path,
    *,
    locator: object,
    expected_sha256: object,
    label: str,
) -> bytes:
    expected = _normalized_sha256(expected_sha256, label=label)
    packaged = _package_payload_by_hash(root, expected_sha256=expected)
    if packaged is not None:
        return packaged
    if not isinstance(locator, str) or not locator:
        raise HostOnlyV4PackageBindingError(
            f"{label} is absent from the package and has no source locator"
        )
    source = Path(locator)
    if not source.is_absolute():
        try:
            _relative, payload = read_package_file_once(root, locator, label=label)
        except HostOnlyV4PackageBindingError:
            payload = None
        if payload is not None and sha256_bytes(payload) == expected:
            return payload
    else:
        payload = read_regular_file_once(source, label=label)
        if sha256_bytes(payload) == expected:
            return payload
    raise HostOnlyV4PackageBindingError(
        f"{label} source bytes do not match the declared sha256"
    )


def _install_package_bytes(root: Path, relative: str, payload: bytes) -> Path:
    path = _canonical_relative(relative, label="binding artifact")
    root_descriptor = _open_directory_chain(root, label="package root")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(root_descriptor)
    try:
        for part in path.parts[:-1]:
            try:
                next_descriptor = os.open(
                    part,
                    directory_flags | nofollow,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(
                    part,
                    directory_flags | nofollow,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            existing_descriptor = os.open(
                path.parts[-1],
                os.O_RDONLY | nofollow,
                dir_fd=descriptor,
            )
        except FileNotFoundError:
            existing_descriptor = None
        if existing_descriptor is not None:
            try:
                info = os.fstat(existing_descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise HostOnlyV4PackageBindingError(
                        f"binding artifact destination is not regular: {relative}"
                    )
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(existing_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                if b"".join(chunks) != payload:
                    raise HostOnlyV4PackageBindingError(
                        f"binding artifact destination drifts: {relative}"
                    )
            finally:
                os.close(existing_descriptor)
        else:
            output_descriptor = os.open(
                path.parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=descriptor,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(output_descriptor, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                os.fsync(output_descriptor)
            finally:
                os.close(output_descriptor)
    except OSError as exc:
        raise HostOnlyV4PackageBindingError(
            f"binding artifact destination is unavailable or unsafe: {relative}"
        ) from exc
    finally:
        os.close(descriptor)
        os.close(root_descriptor)
    return root.joinpath(*path.parts)

def materialize_package_binding(
    *,
    root: Path,
    item: Mapping[str, object],
    generation: Mapping[str, object],
) -> dict[str, object] | None:
    """Materialize portable v4 evidence for a native review-manifest builder.

    Non-v4 generations return ``None``.  V4 generations fail closed unless the
    declared final cover, reference, comparison, and receipt can all be bound to
    package-contained bytes.
    """

    verification = generation.get("final_host_identity_verification")
    if not isinstance(verification, Mapping):
        return None
    if verification.get("schema_version") != HOST_ONLY_SCHEMA_VERSION:
        return None
    if verification.get("authority") != HOST_ONLY_AUTHORITY:
        raise HostOnlyV4PackageBindingError("v4 verification authority is invalid")
    candidate_id = item.get("candidate_id") or item.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise HostOnlyV4PackageBindingError("manifest candidate_id is absent")

    final_rel, final_bytes = read_package_file_once(
        root,
        item.get("cover"),
        label="HOST_ONLY v4 final cover",
    )
    if sha256_bytes(final_bytes) != _normalized_sha256(
        verification.get("final_cover_sha256"), label="final cover"
    ):
        raise HostOnlyV4PackageBindingError(
            "final cover package bytes do not match the v4 receipt"
        )
    witness = verification.get("witness")
    if not isinstance(witness, Mapping):
        raise HostOnlyV4PackageBindingError("v4 receipt witness is absent")
    comparison_bytes = _declared_artifact_bytes(
        root,
        locator=verification.get("comparison_path") or witness.get("image_path"),
        expected_sha256=verification.get("comparison_sha256"),
        label="HOST_ONLY v4 comparison",
    )
    reference_bytes = _declared_artifact_bytes(
        root,
        locator=verification.get("reference_path") or generation.get("reference_image"),
        expected_sha256=verification.get("reference_sha256"),
        label="HOST_ONLY v4 reference",
    )
    receipt_bytes = (
        json.dumps(dict(verification), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    evidence_dir = "evidence"
    receipt_rel = f"{evidence_dir}/{candidate_id}.host-only-v4.json"
    comparison_rel = f"{evidence_dir}/{candidate_id}.host-only-v4-witness.png"
    reference_rel = f"{evidence_dir}/{candidate_id}.host-only-v4-reference.png"
    _install_package_bytes(root, receipt_rel, receipt_bytes)
    _install_package_bytes(root, comparison_rel, comparison_bytes)
    _install_package_bytes(root, reference_rel, reference_bytes)
    binding = build_binding(
        candidate_id=candidate_id,
        receipt_path=receipt_rel,
        receipt_bytes=receipt_bytes,
        comparison_path=comparison_rel,
        comparison_bytes=comparison_bytes,
        reference_path=reference_rel,
        reference_bytes=reference_bytes,
        final_cover_path=final_rel.as_posix(),
        final_cover_bytes=final_bytes,
    )
    probe_item = dict(item)
    probe_item[BINDING_ITEM_KEY] = binding
    validate_package_binding(root=root, item=probe_item, generation=generation)
    return binding
