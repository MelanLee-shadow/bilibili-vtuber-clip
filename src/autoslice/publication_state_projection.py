"""Runtime registry and daily-state projection helpers for publication reconciliation.

The parent module remains the compatibility surface.  These helpers delegate
file primitives back to it at call time so existing tests and callers that
patch those seams continue to observe the same behavior.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

import fcntl

RECONCILIATION_SCHEMA = "publication-reconciliation.v1"
RUNTIME_REGISTRY_SCHEMA = "publication-reconciliation-registry.v1"
PUBLICATION_RECOVERY_SCHEMA = "publication-recovery-projection.v1"
PUBLICATION_RECOVERY_PROJECTION_KIND = "PUBLISHED_TARGET_ONLY"
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PublicationReconciliationError(ValueError):
    """A purported public completion cannot safely update local authority."""


def _reconciliation_module():
    from . import publication_reconciliation

    return publication_reconciliation


def _load_object(path: Path, label: str) -> dict:
    return _reconciliation_module()._load_object(path, label)


def _validate_sha_entry(value: object, label: str) -> Path:
    return _reconciliation_module()._validate_sha_entry(value, label)


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    preserve_state_backup: bool = False,
) -> None:
    _reconciliation_module()._atomic_write_json(
        path,
        payload,
        preserve_state_backup=preserve_state_backup,
    )


def _validate_publication_recovery_projection(
    value: object,
    *,
    recording_date: str,
    verify_authority: bool = True,
) -> list[dict]:
    return _reconciliation_module()._validate_publication_recovery_projection(
        value,
        recording_date=recording_date,
        verify_authority=verify_authority,
    )


def runtime_registry_path(base: Path) -> Path:
    return base.resolve() / "state" / "publication_registry.runtime.v1.json"


def publication_recovery_sidecar_path(base: Path, recording_date: str) -> Path:
    """Return the non-authoritative projection path for a missing daily state.

    The sidecar deliberately lives below a nested directory.  Existing state
    discovery and terminal-date checks only inspect ``state/<date>.json``;
    publishing evidence must therefore never make a missing day appear
    complete by accident.
    """

    if not _DATE_RX.fullmatch(str(recording_date)):
        raise PublicationReconciliationError(
            f"invalid publication recovery recording date: {recording_date}"
        )
    return (
        base.resolve()
        / "state"
        / "publication-recovery"
        / f"{recording_date}.json"
    )


def _state_roots(manifest: Mapping[str, object], base: Path) -> list[Path]:
    roots = {base.resolve()}
    attestation = manifest.get("package_attestation")
    package_root = Path(
        str(attestation.get("package_root") or "")
        if isinstance(attestation, Mapping)
        else ""
    ).resolve()
    parts = package_root.parts
    for index, part in enumerate(parts[:-1]):
        if part == "out" and index + 1 < len(parts):
            roots.add(Path(*parts[:index]))
    return sorted(roots, key=str)


@contextmanager
def _reconciliation_lock(base: Path):
    path = base.resolve() / "state" / "publication-reconciliation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def publication_row_is_verified(row: object) -> bool:
    if not isinstance(row, Mapping) or row.get("status") != "published":
        return False
    publication = row.get("publication_reconciliation")
    structurally_valid = bool(
        isinstance(publication, Mapping)
        and publication.get("schema_version") == RECONCILIATION_SCHEMA
        and publication.get("status")
        in {
            "VERIFIED_PUBLIC",
            "VERIFIED_SAME_BV",
            "VERIFIED_SAME_BV_COVER",
        }
        and str(publication.get("candidate_id") or "")
        == str(row.get("candidate_id") or row.get("cid") or "")
        and str(publication.get("bvid") or "") == str(row.get("bvid") or "")
        and isinstance(publication.get("authority"), Mapping)
    )
    if not structurally_valid:
        return False
    try:
        _validate_sha_entry(
            publication.get("authority"),
            "state publication reconciliation authority",
        )
    except (OSError, PublicationReconciliationError):
        return False
    return True


def project_publication_closure(state: Mapping[str, object]) -> dict:
    rows = [
        row
        for lane in ("picks", "songs")
        for row in (
            state.get(lane) if isinstance(state.get(lane), list) else []
        )
        if isinstance(row, Mapping)
    ]
    published = [row for row in rows if publication_row_is_verified(row)]
    if not published:
        return {
            "schema_version": "daily-publication-closure.v1",
            "status": "NOT_APPLICABLE",
            "published_candidate_ids": [],
            "ready_unpublished_candidate_ids": [],
            "unresolved_candidate_ids": [],
        }
    ready_statuses = {"ok", "review_ready", "quarantine"}
    ready = [
        row
        for row in rows
        if row not in published
        and (
            row.get("status") in ready_statuses
            or bool(row.get("delivered"))
        )
    ]
    unresolved = [row for row in rows if row not in published and row not in ready]
    pending = sum(
        len(state.get(key)) if isinstance(state.get(key), list) else 0
        for key in ("pending_talk", "pending_song")
    )
    if pending:
        status = "publication_in_progress"
    elif ready and unresolved:
        status = "ready_unpublished_with_failures"
    elif ready:
        status = "ready_unpublished"
    elif unresolved:
        status = "published_with_failures"
    else:
        status = "published"
    def candidate(row: Mapping[str, object]) -> str:
        return str(row.get("candidate_id") or row.get("cid") or "")

    return {
        "schema_version": "daily-publication-closure.v1",
        "status": status,
        "published_candidate_ids": sorted(candidate(row) for row in published),
        "ready_unpublished_candidate_ids": sorted(candidate(row) for row in ready),
        "unresolved_candidate_ids": sorted(candidate(row) for row in unresolved),
    }


def _apply_publication_to_state(
    state: dict,
    publication: Mapping[str, object],
) -> bool | None:
    candidate_id = str(publication.get("candidate_id") or "")
    matches: list[dict] = []
    for lane in ("picks", "songs"):
        values = state.get(lane)
        if not isinstance(values, list):
            continue
        matches.extend(
            row
            for row in values
            if isinstance(row, dict)
            and str(row.get("candidate_id") or row.get("cid") or "")
            == candidate_id
        )
    if not matches:
        return None
    if len(matches) != 1:
        raise PublicationReconciliationError(
            f"daily state has {len(matches)} rows for candidate {candidate_id}"
        )
    row = matches[0]
    if publication_row_is_verified(row):
        current = row.get("publication_reconciliation")
        if (
            isinstance(current, Mapping)
            and current.get("bvid") != publication.get("bvid")
        ):
            raise PublicationReconciliationError(
                "daily state publication BVID conflicts with reconciliation"
            )
    changed = False
    if row.get("status") != "published" and "prepublication_status" not in row:
        row["prepublication_status"] = row.get("status")
        changed = True
    intended = {
        "status": "published",
        "rc": 0,
        "bvid": publication.get("bvid"),
        "aid": publication.get("aid"),
        "published_cid": publication.get("cid"),
        "publication_reconciliation": dict(publication),
    }
    for key, value in intended.items():
        if row.get(key) != value:
            row[key] = value
            changed = True
    closure = project_publication_closure(state)
    if state.get("publication_closure") != closure:
        state["publication_closure"] = closure
        changed = True
    if state.get("status") != closure["status"]:
        state["status"] = closure["status"]
        changed = True
    return changed


def _update_state_file(
    state_path: Path,
    publication: Mapping[str, object],
) -> bool:
    if not state_path.is_file():
        return False
    for _attempt in range(5):
        before = state_path.read_bytes()
        try:
            state = json.loads(before.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationReconciliationError(
                f"daily state unreadable: {state_path}: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise PublicationReconciliationError(
                f"daily state is not an object: {state_path}"
            )
        changed = _apply_publication_to_state(state, publication)
        if changed is None:
            return False
        if not changed:
            return True
        if state_path.read_bytes() != before:
            continue
        _atomic_write_json(state_path, state, preserve_state_backup=True)
        return True
    raise PublicationReconciliationError(
        f"daily state changed concurrently too many times: {state_path}"
    )


def _validate_runtime_registry(value: dict) -> list[dict]:
    if value.get("schema_version") != RUNTIME_REGISTRY_SCHEMA:
        raise PublicationReconciliationError(
            "runtime publication registry schema is invalid"
        )
    entries = value.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) for entry in entries
    ):
        raise PublicationReconciliationError(
            "runtime publication registry entries are invalid"
        )
    return entries


def validate_runtime_registry_entry(
    entry: object,
    *,
    verify_authority: bool = True,
) -> dict:
    if not isinstance(entry, dict):
        raise PublicationReconciliationError(
            "runtime publication registry row is invalid"
        )
    publication = entry.get("publication_reconciliation")
    if (
        entry.get("status") != "published"
        or not str(entry.get("candidate_id") or "")
        or not _DATE_RX.fullmatch(str(entry.get("recording_date") or ""))
        or not str(entry.get("bvid") or "")
        or not isinstance(publication, Mapping)
        or publication.get("schema_version") != RECONCILIATION_SCHEMA
        or publication.get("status")
        not in {
            "VERIFIED_PUBLIC",
            "VERIFIED_SAME_BV",
            "VERIFIED_SAME_BV_COVER",
        }
        or publication.get("candidate_id") != entry.get("candidate_id")
        or publication.get("recording_date") != entry.get("recording_date")
        or publication.get("bvid") != entry.get("bvid")
    ):
        raise PublicationReconciliationError(
            "runtime publication registry row is not a verified projection"
        )
    if verify_authority:
        authority_path = _validate_sha_entry(
            publication.get("authority"),
            "runtime publication reconciliation authority",
        )
        authority = _load_object(
            authority_path, "runtime publication reconciliation authority"
        )
        if publication.get("status") == "VERIFIED_PUBLIC":
            valid_authority = (
                authority.get("schema_version")
                == "new-bv-publication-reconciliation-authority.v1"
                and authority.get("status") == "VERIFIED_PUBLIC"
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("recording_date")
                == entry.get("recording_date")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("cid") == publication.get("cid")
            )
        elif publication.get("status") == "VERIFIED_SAME_BV":
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-repair-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("new_cid") == publication.get("cid")
            )
        else:
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-cover-repair-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("unchanged_cid") == publication.get("cid")
            )
        if not valid_authority:
            raise PublicationReconciliationError(
                "runtime publication registry authority content conflicts"
            )
    return entry


def missing_state_publication_reconciliation_block(
    *,
    date: str,
    autoslice_base: Path,
) -> dict[str, object] | None:
    """Describe a missing state that has durable publication evidence.

    This is intentionally read-only.  The runner uses the result as an
    in-memory block so it never manufactures a canonical daily state from a
    publication registry or recovery sidecar.
    """

    base = autoslice_base.resolve()
    state_path = base / "state" / f"{date}.json"
    backup_path = state_path.with_suffix(".json.bak")
    if (
        state_path.exists() or state_path.is_symlink()
        or backup_path.exists() or backup_path.is_symlink()
    ):
        return None
    sidecar_path = publication_recovery_sidecar_path(base, date)
    runtime_path = runtime_registry_path(base)
    sidecar_present = sidecar_path.exists() or sidecar_path.is_symlink()
    runtime_present = runtime_path.exists() or runtime_path.is_symlink()
    runtime_date_present = False
    if runtime_present:
        registry = _load_object(runtime_path, "runtime publication registry")
        entries = _validate_runtime_registry(registry)
        for entry in entries:
            validate_runtime_registry_entry(entry)
            if entry.get("recording_date") == date:
                runtime_date_present = True
    if sidecar_present:
        projection = _load_object(sidecar_path, "publication recovery projection")
        _validate_publication_recovery_projection(
            projection, recording_date=date
        )
    if not sidecar_present and not runtime_date_present:
        return None
    evidence_paths = []
    if sidecar_present:
        evidence_paths.append(str(sidecar_path.resolve()))
    if runtime_date_present:
        evidence_paths.append(str(runtime_path.resolve()))
    return {
        "status": "publication_reconciliation_blocked",
        "publication_reconciliation_error": (
            f"{date}: canonical daily state and .bak are missing while durable "
            "publication evidence exists; original_state_status=MISSING, "
            "original_candidate_set=UNKNOWN"
        ),
        "original_state_status": "MISSING",
        "original_candidate_set": "UNKNOWN",
        "day_completion": "UNKNOWN",
        "candidate_projection_status": PUBLICATION_RECOVERY_PROJECTION_KIND,
        "day_state_status": "UNKNOWN",
        "state_paths": [],
        "publication_recovery_paths": evidence_paths,
    }


def _state_candidate_count(path: Path, candidate_id: str) -> int:
    if not path.is_file():
        return 0
    state = _load_object(path, "daily state")
    return sum(
        1
        for lane in ("picks", "songs")
        for row in (state.get(lane) if isinstance(state.get(lane), list) else [])
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or row.get("cid") or "")
        == candidate_id
    )
