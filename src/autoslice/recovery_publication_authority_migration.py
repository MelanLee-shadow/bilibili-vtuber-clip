"""Exact, directional authority migration for successor same-BV repairs.

Historical repair plans retain the publication registry that authorized their
original CID transition. A later repair normally requires byte-for-byte equal
publication authority. This module permits only one committed, hash-bound
migration from that historical authority to the current combined registry;
there is no generic semantic fallback, reverse edge, or transitive chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Mapping

from src.autoslice.recovery_title_authority import (
    ROOT,
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority_snapshot,
)


POLICY_REPO_PATH = (
    "assets/lidousha/recovery_publication_authority_migrations.v1.json"
)
POLICY_SHA256 = (
    "sha256:c7dd403193d0d067482946f78b9e1e277f54fc41895a0c7eec928ba40bfd79c6"
)
POLICY_SCHEMA = "recovery-publication-authority-migrations.v1"
MIGRATION_SCHEMA = "recovery-publication-authority-migration.v1"
ATTESTATION_SCHEMA = "recovery-publication-authority-migration-attestation.v1"
_ALLOWED_DIFFERING_FIELDS = (
    "authority_sha256",
    "registry_authority",
    "registry_repo_path",
    "registry_sha256",
    "source_public_verify_repo_path",
)
_REQUIRED_EQUAL_FIELDS = (
    "aid",
    "boundary_end_mode",
    "bvid",
    "candidate_id",
    "cid",
    "observed_public_title",
    "registry_schema_version",
    "required_given_end_ms",
    "schema_version",
    "source_kind",
    "source_public_verify_schema_version",
    "source_public_verify_sha256",
    "source_public_verify_status",
    "title_mode",
)
_POLICY_FIELDS = {"schema_version", "authority", "migrations"}
_MIGRATION_FIELDS = {
    "schema_version",
    "migration_id",
    "candidate_id",
    "bvid",
    "aid",
    "original_cid",
    "predecessor_completed_sha256",
    "predecessor_plan_sha256",
    "require_preserve_existing_tags",
    "allowed_differing_fields",
    "required_equal_fields",
    "from_authority",
    "to_authority",
}


class RecoveryPublicationAuthorityMigrationError(ValueError):
    """The exact committed successor-authority migration did not validate."""


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(raw)


def _capture_anchored_file(
    root: Path,
    relative: Path,
    *,
    missing_code: str,
    unsafe_code: str,
    changed_code: str,
) -> bytes:
    """Read one regular file through no-follow directory descriptors."""

    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RecoveryPublicationAuthorityMigrationError(unsafe_code)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    close_fds: list[int] = []
    try:
        directory_fd = os.open(root, directory_flags | nofollow)
        close_fds.append(directory_fd)
        for part in relative.parts[:-1]:
            directory_fd = os.open(
                part,
                directory_flags | nofollow,
                dir_fd=directory_fd,
            )
            close_fds.append(directory_fd)
        file_fd = os.open(
            relative.parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=directory_fd,
        )
        close_fds.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024 * 1024:
            raise RecoveryPublicationAuthorityMigrationError(unsafe_code)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(before) != identity(after):
            raise RecoveryPublicationAuthorityMigrationError(changed_code)
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise RecoveryPublicationAuthorityMigrationError(changed_code)
        return raw
    except RecoveryPublicationAuthorityMigrationError:
        raise
    except FileNotFoundError as exc:
        raise RecoveryPublicationAuthorityMigrationError(missing_code) from exc
    except OSError as exc:
        raise RecoveryPublicationAuthorityMigrationError(unsafe_code) from exc
    finally:
        for descriptor in reversed(close_fds):
            os.close(descriptor)


def _capture_repo_file(
    repo_root: Path,
    repo_path: str,
    *,
    label: str,
) -> bytes:
    return _capture_anchored_file(
        repo_root.resolve(),
        Path(repo_path),
        missing_code=f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_MISSING",
        unsafe_code=f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_UNSAFE",
        changed_code=f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_CHANGED",
    )


def _capture_absolute_file(path: Path, *, label: str) -> bytes:
    if not path.is_absolute():
        raise RecoveryPublicationAuthorityMigrationError(
            f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_UNSAFE"
        )
    return _capture_anchored_file(
        Path(path.anchor),
        path.relative_to(path.anchor),
        missing_code=f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_MISSING",
        unsafe_code=f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_UNSAFE",
        changed_code=f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_{label}_CHANGED",
    )


def _load_policy(*, repo_root: Path) -> tuple[dict[str, object], Path]:
    root = repo_root.resolve()
    policy_path = root / POLICY_REPO_PATH
    raw = _capture_repo_file(repo_root, POLICY_REPO_PATH, label="POLICY")
    if _sha256(raw) != POLICY_SHA256:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_POLICY_HASH_DRIFT"
        )
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_POLICY_JSON_INVALID"
        ) from exc
    if (
        not isinstance(policy, dict)
        or set(policy) != _POLICY_FIELDS
        or policy.get("schema_version") != POLICY_SCHEMA
        or not isinstance(policy.get("authority"), str)
        or not policy["authority"].strip()
        or not isinstance(policy.get("migrations"), list)
        or len(policy["migrations"]) != 1
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_POLICY_SCHEMA_INVALID"
        )
    migration = policy["migrations"][0]
    if (
        not isinstance(migration, dict)
        or set(migration) != _MIGRATION_FIELDS
        or migration.get("schema_version") != MIGRATION_SCHEMA
        or migration.get("allowed_differing_fields")
        != list(_ALLOWED_DIFFERING_FIELDS)
        or migration.get("required_equal_fields")
        != list(_REQUIRED_EQUAL_FIELDS)
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_SCHEMA_INVALID"
        )
    return dict(migration), policy_path


def _validate_captured_endpoint(
    authority: object,
    *,
    candidate_id: str,
    expected_title: str,
    repo_root: Path,
) -> dict[str, object]:
    if not isinstance(authority, Mapping):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_ENDPOINT_INVALID"
        )
    registry_path = str(authority.get("registry_repo_path") or "")
    registry_raw = _capture_repo_file(
        repo_root,
        registry_path,
        label="ENDPOINT_REGISTRY",
    )
    try:
        registry = json.loads(registry_raw.decode("utf-8"))
        raw_entries = registry["entries"]
        source_paths = {
            str(entry["source_public_verify_repo_path"])
            for entry in raw_entries
        }
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_ENDPOINT_INVALID"
        ) from exc
    source_receipts = {
        source_path: _capture_repo_file(
            repo_root,
            source_path,
            label="ENDPOINT_SOURCE",
        )
        for source_path in source_paths
    }
    try:
        return validate_recovery_publication_authority_snapshot(
            authority,
            candidate_id=candidate_id,
            registry_raw=registry_raw,
            captured_source_receipts=source_receipts,
            expected_final_title=expected_title,
            repo_root=repo_root,
        )
    except RecoveryTitleAuthorityError as exc:
        raise RecoveryPublicationAuthorityMigrationError(
            f"RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_ENDPOINT_INVALID:{exc}"
        ) from exc


def validate_successor_authority_migration(
    predecessor_authority: object,
    successor_authority: object,
    *,
    candidate_id: str,
    bvid: str,
    predecessor_plan_sha256: str,
    predecessor_completed_sha256: str,
    preserve_existing_tags: bool,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    """Return a canonical attestation for the one approved migration edge.

    Both endpoints are independently replayed through their native registries
    and public-identity receipts before the exact committed policy is applied.
    """

    if not preserve_existing_tags:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_REQUIRES_TAG_PRESERVATION"
        )
    migration, policy_path = _load_policy(repo_root=repo_root)
    from_authority = migration.get("from_authority")
    to_authority = migration.get("to_authority")
    if not isinstance(from_authority, Mapping) or not isinstance(
        to_authority, Mapping
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_ENDPOINT_INVALID"
        )
    expected_title = str(from_authority.get("observed_public_title") or "")
    validated_predecessor = _validate_captured_endpoint(
        predecessor_authority,
        candidate_id=candidate_id,
        expected_title=expected_title,
        repo_root=repo_root,
    )
    validated_successor = _validate_captured_endpoint(
        successor_authority,
        candidate_id=candidate_id,
        expected_title=expected_title,
        repo_root=repo_root,
    )
    if (
        predecessor_authority != validated_predecessor
        or successor_authority != validated_successor
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_ENDPOINT_NOT_CANONICAL"
        )
    if validated_predecessor != from_authority:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_FROM_MISMATCH"
        )
    if validated_successor != to_authority:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_TO_MISMATCH"
        )
    if (
        migration.get("candidate_id") != candidate_id
        or migration.get("bvid") != bvid
        or migration.get("aid") != validated_predecessor.get("aid")
        or migration.get("original_cid") != validated_predecessor.get("cid")
        or migration.get("predecessor_plan_sha256")
        != predecessor_plan_sha256
        or migration.get("predecessor_completed_sha256")
        != predecessor_completed_sha256
        or migration.get("require_preserve_existing_tags") is not True
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_SCOPE_MISMATCH"
        )
    if (
        validated_successor.get("bvid") != bvid
        or validated_successor.get("aid") != migration.get("aid")
        or validated_successor.get("cid") != migration.get("original_cid")
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_TARGET_MISMATCH"
        )
    predecessor_fields = set(validated_predecessor)
    successor_fields = set(validated_successor)
    if predecessor_fields != successor_fields:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_FIELD_SET_MISMATCH"
        )
    differing = tuple(
        sorted(
            key
            for key in predecessor_fields
            if validated_predecessor[key] != validated_successor[key]
        )
    )
    if differing != _ALLOWED_DIFFERING_FIELDS:
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_DIFF_MISMATCH"
        )
    if tuple(sorted(predecessor_fields - set(differing))) != (
        _REQUIRED_EQUAL_FIELDS
    ):
        raise RecoveryPublicationAuthorityMigrationError(
            "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_EQUAL_FIELDS_MISMATCH"
        )
    for field in _REQUIRED_EQUAL_FIELDS:
        if validated_predecessor[field] != validated_successor[field]:
            raise RecoveryPublicationAuthorityMigrationError(
                "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_IDENTITY_MISMATCH"
            )

    return {
        "schema_version": ATTESTATION_SCHEMA,
        "migration_id": migration["migration_id"],
        "policy": {
            "repo_path": policy_path.relative_to(repo_root.resolve()).as_posix(),
            "sha256": POLICY_SHA256,
        },
        "migration_sha256": _canonical_sha256(migration),
        "from_authority_sha256": validated_predecessor["authority_sha256"],
        "to_authority_sha256": validated_successor["authority_sha256"],
        "predecessor_plan_sha256": "sha256:" + predecessor_plan_sha256,
        "predecessor_completed_sha256": "sha256:" + predecessor_completed_sha256,
        "preserve_existing_tags": True,
    }


def predecessor_authority_migration_binding(
    predecessor_plan: Mapping[str, object],
    successor_authority: Mapping[str, object],
    plan_entry: Mapping[str, object],
    predecessor_completed: Mapping[str, object],
    completed_path: Path,
    bvid: str,
    preserve_existing_tags: bool,
) -> tuple[dict[str, object], str | None]:
    """Return the optional plan binding and a stable validation error."""

    predecessor = predecessor_plan.get("recovery_publication_authority")
    if predecessor == successor_authority:
        return {}, None
    try:
        plan_raw = _capture_absolute_file(
            Path(str(plan_entry.get("path") or "")),
            label="PREDECESSOR_PLAN",
        )
        completed_raw = _capture_absolute_file(
            completed_path,
            label="PREDECESSOR_COMPLETED",
        )
        if (
            json.loads(plan_raw) != predecessor_plan
            or json.loads(completed_raw) != predecessor_completed
        ):
            raise RecoveryPublicationAuthorityMigrationError(
                "RECOVERY_PUBLICATION_AUTHORITY_MIGRATION_PREDECESSOR_SNAPSHOT_MISMATCH"
            )
        attestation = validate_successor_authority_migration(
            predecessor,
            successor_authority,
            candidate_id=str(successor_authority.get("candidate_id") or ""),
            bvid=bvid,
            predecessor_plan_sha256=_sha256(plan_raw).removeprefix("sha256:"),
            predecessor_completed_sha256=_sha256(completed_raw).removeprefix(
                "sha256:"
            ),
            preserve_existing_tags=preserve_existing_tags,
        )
    except (RecoveryPublicationAuthorityMigrationError, ValueError) as exc:
        return {}, str(exc)
    return {"authority_migration": attestation}, None
