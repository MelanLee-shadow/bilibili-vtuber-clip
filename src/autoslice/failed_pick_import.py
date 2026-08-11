"""Narrow authority and state transition for adopting one failed talk pick.

An external package import normally creates a missing pick or rebinds an
already review-ready pick.  This module owns the sole exception: a committed
publication-registry row may authorize one exact failed-row preimage to become
review-ready.  The authority is candidate scoped, hash bound, one-shot, and
does not grant upload permission.

``package_import`` deliberately treats this module as a small domain API: it
loads an authority, asks for an adoption plan before copying bytes, projects
the consumption provenance, and validates the final closure transition.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


FAILED_PICK_IMPORT_AUTHORITY_SCHEMA_VERSION = (
    "failed-pick-import-authority.v1"
)
FAILED_PICK_IMPORT_CONSUMPTION_SCHEMA_VERSION = (
    "failed-pick-import-consumption.v1"
)
FAILED_PICK_IMPORT_SCOPE = "ADOPT_FAILED_PICK_FOR_EXTERNAL_PACKAGE_IMPORT_ONLY"

# These keys describe active failed/retry state that must not survive after
# the exact reviewed package becomes the current delivery.  Historical retry
# evidence stays on the row and the state backup preserves the full preimage.
_ACTIVE_FAILURE_PICK_KEYS = frozenset(
    {
        "failure_evidence",
        "failure_fingerprint",
        "failure_kind",
        "failure_message",
        "failure_recoverable",
        "failure_recovery_fingerprint",
        "failure_stage",
        "rejected_status",
        "rejection_reason",
        "retry_reason",
        "sanctioned_revival_retry",
        "selected_repair",
    }
)


class PackageImportError(RuntimeError):
    """One typed, fail-closed refusal with an operator-actionable hint."""

    def __init__(self, code: str, detail: str, *, hint: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "hint": self.hint}


def canonical_json_sha256(value: object) -> str:
    """Hash one JSON value using the repository's canonical object encoding."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PackageImportError(
            "CANONICAL_JSON_INVALID",
            f"value cannot be canonically encoded as JSON: {error}",
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _declared_digest(value: object, *, label: str) -> str:
    text = str(value or "").removeprefix("sha256:")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PackageImportError(
            "DECLARED_DIGEST_INVALID", f"{label} is not a canonical SHA-256"
        )
    return text


def _require_regular_file(path: Path, *, label: str) -> Path:
    absolute = Path(path).absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PackageImportError(
                "UNSAFE_PATH_SYMLINK",
                f"{label} path contains a symlink: {cursor}",
            )
    try:
        regular = stat.S_ISREG(os.lstat(absolute).st_mode)
    except OSError:
        regular = False
    if not regular:
        raise PackageImportError(
            "SOURCE_FILE_MISSING",
            f"{label} is missing or not a regular file: {path}",
        )
    return absolute


@dataclass(frozen=True)
class FailedPickImportAuthorization:
    """One registry-backed, candidate-scoped failed-pick adoption permit."""

    candidate_id: str
    date: str
    released_by: str
    released_at: str
    release_quote: str
    registry_path: Path
    registry_sha256: str
    registry_bytes: int
    registry_entry_sha256: str
    state_row_canonical_sha256: str
    authority: dict[str, Any]

    def receipt_binding(self) -> dict[str, Any]:
        return {
            "schema_version": FAILED_PICK_IMPORT_AUTHORITY_SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "recording_date": self.date,
            "released_by": self.released_by,
            "released_at": self.released_at,
            "release_quote": self.release_quote,
            "publication_registry": {
                "path": str(self.registry_path),
                "sha256": "sha256:" + self.registry_sha256,
                "bytes": self.registry_bytes,
                "entry_canonical_sha256": (
                    "sha256:" + self.registry_entry_sha256
                ),
            },
            "authority": copy.deepcopy(self.authority),
        }

    def consumption(self, *, consumed_at: str) -> dict[str, Any]:
        return {
            **self.receipt_binding(),
            "schema_version": FAILED_PICK_IMPORT_CONSUMPTION_SCHEMA_VERSION,
            "status": "CONSUMED",
            "consumed_at": consumed_at,
            "preimage_pick_row_canonical_sha256": (
                "sha256:" + self.state_row_canonical_sha256
            ),
        }


@dataclass(frozen=True)
class FailedPickAdoptionPlan:
    """Validated preimage coordinates for one authorized adoption."""

    batch_status: str
    pick_index: int
    failed_pick_authorization: FailedPickImportAuthorization
    will_create_pick_row: bool = False
    will_adopt_failed_pick: bool = True


class PackageBinding(Protocol):
    """Package fields required to project state import provenance."""

    record_path: Path
    publish_path: Path
    burned_path: Path
    burned_video_sha256: str
    cover_path: Path
    cover_sha256: str
    same_stem_cover_path: Path
    journal_path: Path
    journal_sha256: str


def load_failed_pick_import_authorization(
    *,
    registry_path: Path,
    candidate_id: str,
    date: str,
    release_quote: str,
) -> FailedPickImportAuthorization:
    """Resolve the sole committed-registry authority for one failed pick."""

    path = _require_regular_file(
        registry_path, label="committed publication registry"
    )
    payload = path.read_bytes()
    relative = Path("assets/lidousha/publication_registry.v1.json")
    try:
        repo_root = path.parents[2]
    except IndexError as error:
        raise PackageImportError(
            "FAILED_PICK_REGISTRY_AUTHORITY_INVALID",
            "publication registry is outside the canonical repository layout",
        ) from error
    if path != repo_root / relative:
        raise PackageImportError(
            "FAILED_PICK_REGISTRY_AUTHORITY_INVALID",
            "publication registry path is not the canonical repository asset path",
        )
    try:
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=relative,
            observed_bytes=payload,
        )
    except RepositoryAssetAuthorityError as error:
        raise PackageImportError(
            "FAILED_PICK_REGISTRY_AUTHORITY_INVALID",
            f"publication registry is not committed or deployed: {error}",
        ) from error
    try:
        registry = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise PackageImportError(
            "FAILED_PICK_REGISTRY_INVALID",
            f"committed publication registry is unreadable: {error}",
        ) from error
    if (
        not isinstance(registry, Mapping)
        or registry.get("schema_version") != "publication-registry.v1"
        or not isinstance(registry.get("entries"), list)
    ):
        raise PackageImportError(
            "FAILED_PICK_REGISTRY_INVALID",
            "committed publication registry has an invalid schema or entries",
        )
    for index, row in enumerate(registry["entries"]):
        if (
            not isinstance(row, Mapping)
            or not str(row.get("candidate_id") or "").strip()
            or row.get("status")
            not in {"published", "hold_pending_review", "released_for_upload"}
            or (
                row.get("status") == "published"
                and not str(row.get("bvid") or "").strip()
            )
        ):
            raise PackageImportError(
                "FAILED_PICK_REGISTRY_INVALID",
                f"committed publication registry row {index} is invalid",
            )
    matches = [
        row
        for row in registry["entries"]
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or "") == candidate_id
        and str(row.get("recording_date") or "") == date
    ]
    if len(matches) != 1:
        raise PackageImportError(
            "FAILED_PICK_RELEASE_NOT_UNIQUE",
            f"committed publication registry has {len(matches)} rows for "
            f"{candidate_id} on {date}; exactly one is required",
        )
    entry = matches[0]
    if entry.get("status") != "released_for_upload":
        raise PackageImportError(
            "FAILED_PICK_NOT_RELEASED",
            f"registry status={entry.get('status')!r}; released_for_upload required",
        )
    if str(entry.get("bvid") or "").strip():
        raise PackageImportError(
            "FAILED_PICK_RELEASE_IDENTITY_CONFLICT",
            "released failed-pick registry row already carries a BVID",
        )
    if entry.get("released_by") != "Ivan":
        raise PackageImportError(
            "FAILED_PICK_RELEASE_ACTOR_INVALID",
            f"registry released_by={entry.get('released_by')!r}; exact Ivan required",
        )
    released_at = str(entry.get("released_at") or "")
    try:
        datetime.fromisoformat(released_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise PackageImportError(
            "FAILED_PICK_RELEASE_DATE_INVALID",
            "registry released_at must be a valid ISO-8601 date or timestamp",
        ) from error
    if not release_quote or entry.get("release_quote") != release_quote:
        raise PackageImportError(
            "FAILED_PICK_RELEASE_QUOTE_MISMATCH",
            "--release-quote does not exactly match the committed Ivan quote",
        )
    authority = entry.get("failed_pick_import_authority")
    expected_authority_keys = {
        "schema_version",
        "scope",
        "candidate_id",
        "recording_date",
        "state_row_canonical_sha256",
        "required_pick_state",
        "required_batch_state",
        "authorized_transition",
    }
    if (
        not isinstance(authority, Mapping)
        or set(authority) != expected_authority_keys
        or authority.get("schema_version")
        != FAILED_PICK_IMPORT_AUTHORITY_SCHEMA_VERSION
        or authority.get("scope") != FAILED_PICK_IMPORT_SCOPE
        or authority.get("candidate_id") != candidate_id
        or authority.get("recording_date") != date
    ):
        raise PackageImportError(
            "FAILED_PICK_IMPORT_AUTHORITY_INVALID",
            "registry failed-pick-import authority identity/schema/scope is invalid",
        )
    required_pick = authority.get("required_pick_state")
    if not isinstance(required_pick, Mapping) or dict(required_pick) != {
        "status": "failed",
        "rc": 1,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_findings",
        "selected_repair": True,
    }:
        raise PackageImportError(
            "FAILED_PICK_IMPORT_AUTHORITY_INVALID",
            "authority does not bind the exact eligible failed-pick shape",
        )
    required_batch = authority.get("required_batch_state")
    if not isinstance(required_batch, Mapping) or dict(required_batch) != {
        "status": "published_with_failures",
        "publication_closure_status": "published_with_failures",
    }:
        raise PackageImportError(
            "FAILED_PICK_IMPORT_AUTHORITY_INVALID",
            "authority does not bind the exact pre-adoption batch/closure state",
        )
    transition = authority.get("authorized_transition")
    if not isinstance(transition, Mapping) or dict(transition) != {
        "status": "ready_unpublished_with_failures",
        "publication_closure_status": "ready_unpublished_with_failures",
    }:
        raise PackageImportError(
            "FAILED_PICK_IMPORT_AUTHORITY_INVALID",
            "authority does not bind the exact post-adoption batch/closure state",
        )
    row_sha256 = _declared_digest(
        authority.get("state_row_canonical_sha256"),
        label="failed-pick authority state row",
    )
    return FailedPickImportAuthorization(
        candidate_id=candidate_id,
        date=date,
        released_by="Ivan",
        released_at=released_at,
        release_quote=release_quote,
        registry_path=path,
        registry_sha256=hashlib.sha256(payload).hexdigest(),
        registry_bytes=len(payload),
        registry_entry_sha256=canonical_json_sha256(dict(entry)),
        state_row_canonical_sha256=row_sha256,
        authority=copy.deepcopy(dict(authority)),
    )


def plan_failed_pick_adoption(
    before_state: Mapping[str, Any],
    *,
    candidate_id: str,
    date: str,
    allow_new_pick: bool,
    project_closure: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    authorization: FailedPickImportAuthorization,
) -> FailedPickAdoptionPlan:
    """Validate the exact authorized failed-row and closure preimage."""

    batch_status = str(before_state.get("status") or "")
    if allow_new_pick:
        raise PackageImportError(
            "FAILED_PICK_IMPORT_MODE_CONFLICT",
            "failed-pick adoption cannot be combined with allow-new-pick",
        )
    if authorization.candidate_id != candidate_id or authorization.date != date:
        raise PackageImportError(
            "FAILED_PICK_IMPORT_AUTHORITY_MISMATCH",
            "failed-pick authority does not match the requested candidate/date",
        )
    expected_batch = authorization.authority["required_batch_state"]
    if batch_status != expected_batch["status"]:
        raise PackageImportError(
            "FAILED_PICK_BATCH_PREIMAGE_MISMATCH",
            f"state.status={batch_status!r}; authority requires "
            f"{expected_batch['status']!r}",
        )
    state_date = str(before_state.get("date") or "")
    if state_date and state_date != date:
        raise PackageImportError(
            "STATE_DATE_MISMATCH",
            f"state declares date={state_date!r}, import was asked for {date!r}",
        )
    picks = before_state.get("picks")
    if not isinstance(picks, list):
        raise PackageImportError("STATE_SHAPE_INVALID", "state.picks is not a list")
    matches = [
        index
        for index, row in enumerate(picks)
        if isinstance(row, Mapping) and row.get("candidate_id") == candidate_id
    ]
    if len(matches) > 1:
        raise PackageImportError(
            "DUPLICATE_PICK_ROW",
            f"{candidate_id} already appears {len(matches)} times in picks",
        )
    for queue in ("pending_talk", "pending_song"):
        rows = before_state.get(queue)
        if _collection_contains(rows, candidate_id):
            raise PackageImportError(
                "CANDIDATE_STILL_QUEUED",
                f"{candidate_id} is still queued in {queue}; the runner would "
                "produce over the import",
            )
    closure = project_closure(before_state)
    if candidate_id in list(closure.get("published_candidate_ids") or []):
        raise PackageImportError(
            "CANDIDATE_ALREADY_PUBLISHED",
            f"{candidate_id} is already published; import refuses to rebind a "
            "published delivery",
            hint="published candidates only accept the same-BV edit/replace lane "
            "(publication registry is the sole upload authority)",
        )
    if not matches:
        raise PackageImportError(
            "FAILED_PICK_ROW_ABSENT",
            f"{candidate_id} has no existing failed pick row to adopt",
        )
    for collection in (
        "pending_talk",
        "pending_song",
        "songs",
        "published",
        "published_talk",
        "published_song",
    ):
        if _collection_contains(before_state.get(collection), candidate_id):
            raise PackageImportError(
                "FAILED_PICK_ADOPTION_SCOPE_CONFLICT",
                f"{candidate_id} also appears in state.{collection}",
            )
    row = picks[matches[0]]
    required_pick = authorization.authority["required_pick_state"]
    mismatches = {
        key: {"actual": row.get(key), "expected": expected}
        for key, expected in required_pick.items()
        if row.get(key) != expected
        or (key == "selected_repair" and row.get(key) is not True)
    }
    if mismatches:
        raise PackageImportError(
            "FAILED_PICK_ROW_SHAPE_MISMATCH",
            "failed pick no longer has the exact authorized shape: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True),
        )
    actual_row_sha256 = canonical_json_sha256(dict(row))
    if actual_row_sha256 != authorization.state_row_canonical_sha256:
        raise PackageImportError(
            "FAILED_PICK_ROW_PREIMAGE_DRIFT",
            "failed pick canonical SHA drifted from the committed authority: "
            f"actual=sha256:{actual_row_sha256} authorized=sha256:"
            f"{authorization.state_row_canonical_sha256}",
        )
    required_closure_status = expected_batch["publication_closure_status"]
    stored_closure = before_state.get("publication_closure")
    if (
        not isinstance(stored_closure, Mapping)
        or stored_closure.get("status") != required_closure_status
        or dict(stored_closure) != dict(closure)
    ):
        raise PackageImportError(
            "FAILED_PICK_CLOSURE_PREIMAGE_MISMATCH",
            "stored/projected publication closure is not the exact "
            f"authorized {required_closure_status!r} preimage",
        )
    return FailedPickAdoptionPlan(
        batch_status=batch_status,
        pick_index=matches[0],
        failed_pick_authorization=authorization,
    )


def _collection_contains(rows: object, candidate_id: str) -> bool:
    return isinstance(rows, list) and any(
        isinstance(item, Mapping)
        and str(item.get("candidate_id") or item.get("cid") or "") == candidate_id
        for item in rows
    )


def failed_pick_cleanup_keys(
    authorization: FailedPickImportAuthorization | None,
) -> frozenset[str]:
    """Return active failure keys removable only during an adoption."""

    return _ACTIVE_FAILURE_PICK_KEYS if authorization is not None else frozenset()


def project_failed_pick_provenance(
    *,
    package: PackageBinding,
    schema_version: str,
    created_pick_row: bool,
    previous_import: object,
    authorization: FailedPickImportAuthorization | None,
    consumed_at: str,
) -> dict[str, Any]:
    """Attach new consumption or preserve a prior one-shot consumption."""

    projected: dict[str, Any] = {
        "schema_version": schema_version,
        "status": "VERIFIED_PACKAGE_BOUND",
        "bound_at": consumed_at,
        "created_pick_row": created_pick_row,
        "record_path": str(package.record_path),
        "publish_path": str(package.publish_path),
        "burned_video_path": str(package.burned_path),
        "burned_video_sha256": package.burned_video_sha256,
        "cover_path": str(package.cover_path),
        "cover_sha256": package.cover_sha256,
        "same_stem_cover_path": str(package.same_stem_cover_path),
        "package_relocation_journal_path": str(package.journal_path),
        "package_relocation_journal_sha256": package.journal_sha256,
    }
    if authorization is not None:
        projected["failed_pick_adoption"] = authorization.consumption(
            consumed_at=consumed_at
        )
    elif isinstance(previous_import, Mapping) and isinstance(
        previous_import.get("failed_pick_adoption"), Mapping
    ):
        projected["failed_pick_adoption"] = copy.deepcopy(
            previous_import["failed_pick_adoption"]
        )
    return projected


def apply_failed_pick_transition(
    *,
    before_state: Mapping[str, Any],
    after_state: MutableMapping[str, Any],
    after_closure: Mapping[str, Any],
    candidate_id: str,
    authorization: FailedPickImportAuthorization | None,
) -> None:
    """Validate and apply the sole authorized batch/closure post-transition."""

    if authorization is None:
        return
    transition = authorization.authority["authorized_transition"]
    required_closure = transition["publication_closure_status"]
    if after_closure.get("status") != required_closure:
        raise PackageImportError(
            "FAILED_PICK_CLOSURE_TRANSITION_MISMATCH",
            "adoption projected publication closure status="
            f"{after_closure.get('status')!r}; authority requires "
            f"{required_closure!r}",
        )
    ready_ids = list(after_closure.get("ready_unpublished_candidate_ids") or [])
    if ready_ids.count(candidate_id) != 1:
        raise PackageImportError(
            "FAILED_PICK_CLOSURE_TRANSITION_MISMATCH",
            "adopted candidate is not uniquely ready_unpublished in the "
            "projected closure",
        )
    after_state["status"] = transition["status"]
    for key in set(before_state) | set(after_state):
        if key in {"picks", "status", "publication_closure", "updated_at"}:
            continue
        if after_state.get(key) != before_state.get(key):
            raise PackageImportError(
                "STATE_COLLECTION_MUTATED",
                f"failed-pick adoption changed unrelated state field {key}",
            )


def failed_pick_authority_receipt(
    authorization: FailedPickImportAuthorization | None,
) -> dict[str, Any] | None:
    """Project an optional authority into the state-bind delta receipt."""

    return authorization.receipt_binding() if authorization is not None else None
