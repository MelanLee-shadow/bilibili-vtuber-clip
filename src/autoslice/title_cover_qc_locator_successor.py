"""Create a zero-provider title/cover QC successor for relocated identical bytes.

The original CPA observation remains the authority.  This module permits only
locator rebinding after both the source and target packages independently pass
the existing native title/cover QC consumer and all bound image bytes match.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from src.autoslice.host_only_v4_package_binding import read_package_file_once
from src.autoslice.original_patch_package import json_file, sha_file


SUCCESSOR_SCHEMA = "lidousha-title-cover-joint-qc-locator-successor.v1"


class TitleCoverQcSuccessorError(RuntimeError):
    """The frozen QC cannot be safely rebound to the target package."""


def _reuse_valid_qc(package_root: Path, title: str, receipt_path: Path) -> dict:
    from scripts.run_title_cover_joint_qc import reuse_valid_qc

    return reuse_valid_qc(package_root, title, receipt_path)


def _resolve_package_inputs(package_root: Path, title: str):
    from scripts.run_title_cover_joint_qc import resolve_package_inputs

    return resolve_package_inputs(package_root, title)


def _resolve_candidate_id(record: dict, package_root: Path) -> str:
    from scripts.run_title_cover_joint_qc import resolve_candidate_id

    review = json_file(package_root / "review_manifest.json")
    return resolve_candidate_id(record, review)


def _strip_sha(value: object, *, label: str) -> str:
    raw = str(value or "")
    if raw.startswith("sha256:"):
        raw = raw[len("sha256:") :]
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw.lower()):
        raise TitleCoverQcSuccessorError(f"{label} is not a SHA-256 digest")
    return raw.lower()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TitleCoverQcSuccessorError(f"{label} is missing")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise TitleCoverQcSuccessorError(f"{label} is not a safe package-relative path")
    return path.as_posix()


def _changed_paths(before: Any, after: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changed: set[tuple[str, ...]] = set()
        keys = set(before) | set(after)
        for key in keys:
            child = prefix + (str(key),)
            if key not in before or key not in after:
                changed.add(child)
            else:
                changed.update(_changed_paths(before[key], after[key], child))
        return changed
    if before != after:
        return {prefix}
    return set()


def _root(path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute.anchor, directory_flags)
    except OSError as exc:
        raise TitleCoverQcSuccessorError(f"{label} is unavailable or traverses a symlink") from exc
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise TitleCoverQcSuccessorError(f"{label} has an unsafe path component")
            next_descriptor = os.open(
                component,
                directory_flags | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise TitleCoverQcSuccessorError(f"{label} is not a directory")
        return absolute
    except OSError as exc:
        raise TitleCoverQcSuccessorError(f"{label} is unavailable or traverses a symlink") from exc
    finally:
        os.close(descriptor)


def _reference_binding(
    *,
    receipt: dict,
    source_root: Path,
    destination_root: Path,
    cover_sha256: str,
) -> tuple[dict | None, tuple[str, bytes] | None]:
    witness = receipt.get("witness")
    if not isinstance(witness, dict):
        raise TitleCoverQcSuccessorError("source QC witness is missing")
    reference_image = witness.get("reference_image")
    source_reference = receipt.get("source_reference")
    if reference_image is None and source_reference is None:
        return None, None
    if not isinstance(reference_image, dict) or not isinstance(source_reference, dict):
        raise TitleCoverQcSuccessorError(
            "reference image and source_reference must be declared together"
        )
    if source_reference.get("schema_version") != "joint-qc-package-source-reference.v1":
        raise TitleCoverQcSuccessorError("source_reference schema is not supported")
    if _strip_sha(source_reference.get("cover_sha256"), label="reference cover hash") != (
        cover_sha256
    ):
        raise TitleCoverQcSuccessorError("source_reference cover hash differs")
    relative = _safe_relative(source_reference.get("reference_path"), label="source reference path")
    _source_relative, source_bytes = read_package_file_once(
        source_root,
        relative,
        label="source title/cover QC reference image",
    )
    destination_relative, destination_bytes = read_package_file_once(
        destination_root,
        relative,
        label="destination title/cover QC reference image",
    )
    expected = _strip_sha(source_reference.get("reference_sha256"), label="source reference hash")
    witness_expected = _strip_sha(
        reference_image.get("image_sha256"), label="witness reference hash"
    )
    actual = _sha256_bytes(source_bytes)
    if actual != expected or actual != witness_expected or destination_bytes != source_bytes:
        raise TitleCoverQcSuccessorError("reference image bytes or hashes differ")
    source_image_path = Path(str(reference_image.get("image_path") or "")).absolute()
    expected_source_path = source_root / relative
    if source_image_path.resolve(strict=True) != expected_source_path.resolve(strict=True):
        raise TitleCoverQcSuccessorError("witness reference path differs from source package")
    projected = copy.deepcopy(reference_image)
    projected["image_path"] = str((destination_root / destination_relative).resolve(strict=True))
    return projected, (relative, source_bytes)


def build_title_cover_qc_locator_successor(
    *,
    source_package_root: Path,
    source_receipt_path: Path,
    destination_package_root: Path,
    title: str,
) -> dict:
    """Return an in-memory locator successor after source/target byte validation."""

    source_root = _root(source_package_root, label="source package root")
    destination_root = _root(destination_package_root, label="destination package root")
    source_receipt_absolute = source_receipt_path.absolute()
    source_receipt_info = os.lstat(source_receipt_absolute)
    if stat.S_ISLNK(source_receipt_info.st_mode) or not stat.S_ISREG(source_receipt_info.st_mode):
        raise TitleCoverQcSuccessorError("source QC receipt is not a regular file")
    source_receipt_path = source_receipt_absolute.resolve(strict=True)
    if source_receipt_path.parent != source_root:
        raise TitleCoverQcSuccessorError("source QC receipt is not a package-root file")

    source_receipt_sha = sha_file(source_receipt_path)
    try:
        source_receipt = _reuse_valid_qc(source_root, title, source_receipt_path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise TitleCoverQcSuccessorError(f"source native QC replay failed: {exc}") from exc
    if source_receipt != json_file(source_receipt_path):
        raise TitleCoverQcSuccessorError("source QC replay differs from stored receipt")

    source_record, _source_publish, source_cover = _resolve_package_inputs(source_root, title)
    destination_record, _destination_publish, destination_cover = _resolve_package_inputs(
        destination_root, title
    )
    source_candidate = _resolve_candidate_id(source_record, source_root)
    destination_candidate = _resolve_candidate_id(destination_record, destination_root)
    if source_candidate != destination_candidate:
        raise TitleCoverQcSuccessorError("source and destination candidates differ")
    if source_receipt.get("candidate_id") != source_candidate:
        raise TitleCoverQcSuccessorError("source receipt candidate differs")

    source_cover_bytes = source_cover.read_bytes()
    destination_cover_bytes = destination_cover.read_bytes()
    source_cover_sha = _sha256_bytes(source_cover_bytes)
    if destination_cover_bytes != source_cover_bytes:
        raise TitleCoverQcSuccessorError("source and destination cover bytes differ")
    if _strip_sha(source_receipt.get("cover_sha256"), label="QC cover hash") != (source_cover_sha):
        raise TitleCoverQcSuccessorError("source receipt cover hash differs")
    witness = source_receipt.get("witness")
    if not isinstance(witness, dict):
        raise TitleCoverQcSuccessorError("source receipt witness is missing")
    if _strip_sha(witness.get("image_sha256"), label="QC witness image hash") != (source_cover_sha):
        raise TitleCoverQcSuccessorError("source witness image hash differs")

    projected_reference, reference_binding = _reference_binding(
        receipt=source_receipt,
        source_root=source_root,
        destination_root=destination_root,
        cover_sha256=source_cover_sha,
    )

    projected = copy.deepcopy(source_receipt)
    destination_cover_path = str(destination_cover.resolve(strict=True))
    projected["cover_path"] = destination_cover_path
    projected_witness = projected.get("witness")
    if not isinstance(projected_witness, dict):
        raise TitleCoverQcSuccessorError("projected witness is missing")
    projected_witness["image_path"] = destination_cover_path
    if projected_reference is not None:
        projected_witness["reference_image"] = projected_reference
    projected["locator_successor"] = {
        "schema_version": SUCCESSOR_SCHEMA,
        "source_receipt_name": source_receipt_path.name,
        "source_receipt_sha256": "sha256:" + source_receipt_sha,
        "destination_cover_name": destination_cover.name,
        "cover_sha256": "sha256:" + source_cover_sha,
        "reference_sha256": (
            "sha256:" + _sha256_bytes(reference_binding[1])
            if reference_binding is not None
            else None
        ),
        "provider_calls": 0,
        "projected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    changed = _changed_paths(source_receipt, projected)
    if any(path and path[0] == "locator_successor" for path in changed):
        # A successor may itself be the source authority.  Treat its lineage
        # envelope atomically: the new envelope binds the complete previous
        # receipt by SHA-256, while every observed QC field remains immutable.
        changed = {
            path for path in changed if not path or path[0] != "locator_successor"
        }
        changed.add(("locator_successor",))
    allowed = {
        ("cover_path",),
        ("witness", "image_path"),
        ("locator_successor",),
    }
    if projected_reference is not None:
        allowed.add(("witness", "reference_image", "image_path"))
    if changed != allowed:
        rendered = sorted("/" + "/".join(path) for path in changed)
        raise TitleCoverQcSuccessorError(
            "QC successor changed an unapproved field: " + ", ".join(rendered)
        )
    if sha_file(source_receipt_path) != source_receipt_sha:
        raise TitleCoverQcSuccessorError("source QC receipt changed during projection")
    return projected


def resolve_portable_qc_authority(
    *,
    portable_package_root: Path,
    title: str,
    candidate_id: str,
) -> tuple[Path, Path] | None:
    """Resolve an unchanged generic receipt back to its producer package.

    A portable copy may preserve ``title-cover-joint-qc.json`` byte-for-byte
    while its frozen cover locator still names the package where CPA observed
    the pixels.  The producer receipt must still be regular and byte-identical.
    """

    portable_root = _root(portable_package_root, label="portable package root")
    candidate_receipt = portable_root / f"{candidate_id}.title-cover-joint-qc.json"
    generic_receipt = portable_root / "title-cover-joint-qc.json"
    portable_receipt = (
        candidate_receipt if os.path.lexists(candidate_receipt) else generic_receipt
    )
    if not os.path.lexists(portable_receipt):
        return None
    info = os.lstat(portable_receipt)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TitleCoverQcSuccessorError("portable QC receipt is not a regular file")
    receipt = json_file(portable_receipt)
    if (
        receipt.get("schema_version") != "lidousha-title-cover-joint-qc.v1"
        or receipt.get("candidate_id") != candidate_id
        or receipt.get("title") != title
        or receipt.get("status") != "PASS"
        or receipt.get("pass") is not True
    ):
        raise TitleCoverQcSuccessorError(
            "portable QC receipt does not bind this candidate/title PASS"
        )
    cover_raw = receipt.get("cover_path")
    if not isinstance(cover_raw, str) or not Path(cover_raw).is_absolute():
        raise TitleCoverQcSuccessorError(
            "portable QC receipt has no absolute authority cover path"
        )
    _record, _publish, portable_cover = _resolve_package_inputs(portable_root, title)
    authority_cover = Path(cover_raw).absolute()
    if authority_cover.name != portable_cover.name:
        raise TitleCoverQcSuccessorError(
            "portable QC cover basename differs from package cover"
        )
    portable_cover_sha = _sha256_bytes(portable_cover.read_bytes())
    if _strip_sha(receipt.get("cover_sha256"), label="portable QC cover hash") != (
        portable_cover_sha
    ):
        raise TitleCoverQcSuccessorError(
            "portable QC cover hash differs from package cover"
        )
    authority_root = _root(authority_cover.parent, label="QC authority package root")
    authority_receipt = authority_root / portable_receipt.name
    authority_info = os.lstat(authority_receipt)
    if stat.S_ISLNK(authority_info.st_mode) or not stat.S_ISREG(authority_info.st_mode):
        raise TitleCoverQcSuccessorError("QC authority receipt is not a regular file")
    if authority_receipt.read_bytes() != portable_receipt.read_bytes():
        raise TitleCoverQcSuccessorError(
            "portable QC receipt differs from authority receipt"
        )
    return authority_root, authority_receipt


def _cleanup_owned_output(
    *,
    parent_fd: int,
    name: str,
    device: int,
    inode: int,
    expected_sha256: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != (device, inode):
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    if _sha256_bytes(b"".join(chunks)) == expected_sha256:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)


def _replay_source_authority_unchanged(
    *,
    source_package_root: Path,
    source_receipt_path: Path,
    title: str,
    expected_sha256: str,
) -> dict:
    """Replay the source gate and prove its authority bytes stayed frozen."""

    source_root = _root(source_package_root, label="source package root")
    source_receipt_absolute = source_receipt_path.absolute()
    source_receipt_info = os.lstat(source_receipt_absolute)
    if stat.S_ISLNK(source_receipt_info.st_mode) or not stat.S_ISREG(source_receipt_info.st_mode):
        raise TitleCoverQcSuccessorError("source QC authority changed after projection")
    source_receipt = source_receipt_absolute.resolve(strict=True)
    if source_receipt.parent != source_root:
        raise TitleCoverQcSuccessorError("source QC authority changed after projection")
    if sha_file(source_receipt) != expected_sha256:
        raise TitleCoverQcSuccessorError("source QC authority changed after projection")
    replayed = _reuse_valid_qc(source_root, title, source_receipt)
    if replayed != json_file(source_receipt):
        raise TitleCoverQcSuccessorError("source QC authority changed during final replay")
    if sha_file(source_receipt) != expected_sha256:
        raise TitleCoverQcSuccessorError("source QC authority changed during final replay")
    return replayed


def create_title_cover_qc_locator_successor(
    *,
    source_package_root: Path,
    source_receipt_path: Path,
    destination_package_root: Path,
    output_path: Path,
    title: str,
) -> dict:
    """Create and natively replay a target-bound receipt without provider calls."""

    from scripts.run_title_cover_joint_qc import (
        preflight_create_only_output,
        write_receipt_create_only,
    )

    destination_root = _root(destination_package_root, label="destination package root")
    projected = build_title_cover_qc_locator_successor(
        source_package_root=source_package_root,
        source_receipt_path=source_receipt_path,
        destination_package_root=destination_root,
        title=title,
    )
    candidate_id = str(projected.get("candidate_id") or "")
    expected_name = f"{candidate_id}.title-cover-joint-qc.json"
    absolute_output = output_path.absolute()
    if absolute_output.parent.resolve(strict=True) != destination_root:
        raise TitleCoverQcSuccessorError("QC successor output is outside target package")
    if absolute_output.name != expected_name:
        raise TitleCoverQcSuccessorError(f"QC successor output must be named {expected_name}")

    serialized = (json.dumps(projected, ensure_ascii=False, indent=1) + "\n").encode()
    expected_sha = _sha256_bytes(serialized)
    parent_fd, name = preflight_create_only_output(absolute_output)
    created: os.stat_result | None = None
    try:
        write_receipt_create_only(parent_fd, name, projected)
        created = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        try:
            replayed = _reuse_valid_qc(destination_root, title, absolute_output)
            if replayed != projected:
                raise ValueError("target replay differs from projected receipt")
            locator_successor = projected.get("locator_successor")
            if not isinstance(locator_successor, dict):
                raise TitleCoverQcSuccessorError("projected QC lacks its source authority binding")
            _replay_source_authority_unchanged(
                source_package_root=source_package_root,
                source_receipt_path=source_receipt_path,
                title=title,
                expected_sha256=_strip_sha(
                    locator_successor.get("source_receipt_sha256"),
                    label="source QC authority hash",
                ),
            )
            try:
                final_target = _reuse_valid_qc(destination_root, title, absolute_output)
            except (OSError, ValueError, RuntimeError) as exc:
                raise TitleCoverQcSuccessorError(
                    f"destination QC package changed during final source replay: {exc}"
                ) from exc
            if final_target != projected:
                raise TitleCoverQcSuccessorError(
                    "destination QC package changed during final source replay"
                )
        except (OSError, ValueError, RuntimeError) as exc:
            if isinstance(exc, TitleCoverQcSuccessorError):
                raise
            raise TitleCoverQcSuccessorError(
                f"target replay, final source replay, or final target replay failed: {exc}"
            ) from exc
        if sha_file(absolute_output) != expected_sha:
            raise TitleCoverQcSuccessorError("QC successor changed after target replay")
        return replayed
    except Exception:
        if created is not None:
            _cleanup_owned_output(
                parent_fd=parent_fd,
                name=name,
                device=created.st_dev,
                inode=created.st_ino,
                expected_sha256=expected_sha,
            )
        raise
    finally:
        os.close(parent_fd)
