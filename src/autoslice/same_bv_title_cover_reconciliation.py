"""Create-only consumption receipt for a verified title-and-cover same-BV edit.

The BVID and CID are unchanged, so this consumer does not impersonate the full
media or cover-only reconciliation schemas.  It binds the strict authority,
plan, journal and fresh four-surface completed receipt in one auditable no-op
identity projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice import same_bv_title_cover_journal as journal_binding
from src.autoslice.same_bv_title_cover_authority import load_authority
from src.autoslice.same_bv_title_cover_plan import load_plan, validate_plan
from src.autoslice.same_bv_title_cover_repair import status as repair_status
from src.autoslice.same_bv_title_cover_verification import COMPLETED_SCHEMA


SCHEMA_VERSION = "same-bv-title-cover-publication-reconciliation.v1"


class TitleCoverReconciliationError(RuntimeError):
    """The completed title-cover transaction is not safe to consume."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regular(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TitleCoverReconciliationError(f"{label} unavailable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TitleCoverReconciliationError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TitleCoverReconciliationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TitleCoverReconciliationError(f"{label} root must be an object")
    return value


def _values_for_key(value: object, key: str) -> list[object]:
    result: list[object] = []
    if isinstance(value, Mapping):
        for current_key, current_value in value.items():
            if current_key == key:
                result.append(current_value)
            result.extend(_values_for_key(current_value, key))
    elif isinstance(value, list):
        for item in value:
            result.extend(_values_for_key(item, key))
    return result


def _unique_string(value: object, key: str) -> str:
    values = [item for item in _values_for_key(value, key) if isinstance(item, str) and item]
    unique = set(values)
    if len(unique) != 1:
        raise TitleCoverReconciliationError(
            f"{key} must have one unambiguous non-empty value; observed={sorted(unique)!r}"
        )
    return next(iter(unique))


def _unique_integer(value: object, key: str) -> int:
    values = [item for item in _values_for_key(value, key) if type(item) is int]
    unique = set(values)
    if len(unique) != 1:
        raise TitleCoverReconciliationError(
            f"{key} must have one unambiguous integer value; observed={sorted(unique)!r}"
        )
    return next(iter(unique))



def _target_title(authority: Mapping[str, Any]) -> str:
    target = authority.get("target")
    if not isinstance(target, Mapping):
        raise TitleCoverReconciliationError("authority target must be an object")
    title = target.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TitleCoverReconciliationError("authority target title is missing")
    return title

def _descriptor(path: Path) -> dict[str, object]:
    path = _regular(path, str(path))
    return {"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size}


def reconcile(
    *,
    authority_path: Path,
    plan_path: Path,
    journal: Path,
    completed_path: Path,
    out: Path,
    reconciled_at: str,
) -> dict[str, Any]:
    """Consume one fresh completed receipt without rewriting identity registries."""

    authority_path = _regular(Path(authority_path), "title-cover authority")
    plan_path = _regular(Path(plan_path), "title-cover plan")
    journal = _regular(Path(journal), "title-cover journal")
    completed_path = _regular(Path(completed_path), "title-cover completed receipt")
    authority = load_authority(authority_path)
    plan = load_plan(plan_path)
    validate_plan(plan, authority=authority, plan_path=plan_path)
    state = repair_status(plan_path=plan_path, journal=journal)
    if state.state != "VERIFIED":
        raise TitleCoverReconciliationError(
            f"title-cover journal is not VERIFIED: {state.state}: {state.message}"
        )
    rows = journal_binding.plan_rows(journal, plan_path, plan)
    if not rows or rows[-1].get("state") != "VERIFIED":
        raise TitleCoverReconciliationError("title-cover journal has no bound VERIFIED row")
    terminal = rows[-1]
    completed = _load_json(completed_path, "title-cover completed receipt")
    if completed.get("schema_version") != COMPLETED_SCHEMA:
        raise TitleCoverReconciliationError(
            f"completed receipt schema must be {COMPLETED_SCHEMA!r}"
        )
    if completed.get("status") != "VERIFIED_FRESH_LIVE" or completed.get("remote_mutation") is not False:
        raise TitleCoverReconciliationError("completed receipt is not a fresh no-mutation closure")

    authority_bvid = _unique_string(authority, "bvid")
    plan_bvid = _unique_string(plan, "bvid")
    completed_bvid = _unique_string(completed, "bvid")
    if not (authority_bvid == plan_bvid == completed_bvid):
        raise TitleCoverReconciliationError("BVID differs across authority/plan/completed")
    authority_cid = _unique_integer(authority, "cid")
    completed_cid = _unique_integer(completed, "cid")
    if authority_cid != completed_cid:
        raise TitleCoverReconciliationError("CID changed during a title-cover-only revision")
    target_title = _target_title(authority)

    expected_plan = {
        "path": str(plan_path),
        "sha256": _sha256(plan_path),
        "plan_id": plan.get("plan_id"),
    }
    if completed.get("plan") != expected_plan:
        raise TitleCoverReconciliationError("completed plan binding differs")
    if completed.get("authority") != plan.get("authority"):
        raise TitleCoverReconciliationError("completed authority binding differs")
    terminal_claim = completed.get("verified_journal_row") or {}
    if any(
        terminal_claim.get(key) != terminal.get(key)
        for key in ("seq", "at", "row_sha256")
    ):
        raise TitleCoverReconciliationError("completed VERIFIED journal row differs")
    terminal_snapshot = (terminal.get("details") or {}).get("live_snapshot")
    if completed.get("live_snapshot") != terminal_snapshot:
        raise TitleCoverReconciliationError("completed live snapshot differs from VERIFIED row")
    expected_completed = {
        "candidate_id": plan.get("candidate_id"),
        "aid": plan.get("aid"),
        "unchanged_cid": authority_cid,
        "old_title": plan.get("old_title"),
        "new_title": target_title,
        "new_cover": plan.get("replacement_cover"),
        "media_identity": plan.get("media_identity"),
    }
    for key, expected in expected_completed.items():
        if completed.get(key) != expected:
            raise TitleCoverReconciliationError(f"completed {key} differs from plan")

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "CONSUMED",
        "reconciled_at": reconciled_at,
        "public_identity": {"bvid": authority_bvid, "cid": authority_cid},
        "target_title": target_title,
        "authority": _descriptor(authority_path),
        "plan": _descriptor(plan_path),
        "journal": _descriptor(journal),
        "completed": _descriptor(completed_path),
        "projection": {
            "kind": "UNCHANGED_BVID_CID_TITLE_COVER_ONLY",
            "registry_identity_mutation_required": False,
            "daily_state_identity_mutation_required": False,
            "metadata_authority": "THIS_RECONCILIATION_RECEIPT",
        },
    }
    out = Path(out)
    if out.exists() or out.is_symlink():
        existing = _load_json(out, "title-cover reconciliation receipt")
        comparable = dict(receipt)
        comparable["reconciled_at"] = existing.get("reconciled_at")
        if existing != comparable:
            raise TitleCoverReconciliationError("reconciliation output already exists with different bytes")
        return existing
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return receipt
