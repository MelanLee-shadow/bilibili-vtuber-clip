"""Idempotent post-publication reconciliation for registry and runner state.

The upload ledger is intentionally not an authority here.  New-BV
reconciliation accepts only the hash-bound manifest plus the complete
``VERIFIED_PUBLIC``/season/uploaded evidence closure.  Same-BV reconciliation
accepts only a create-only ``same-bv-repair-completed.v1`` receipt, or the
narrow unchanged-CID ``same-bv-cover-repair-completed.v1`` receipt.  Each
successful reconciliation writes a durable runtime registry overlay before it
projects the same truth into the deployed registry and every matching daily
state file.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
from collections.abc import Mapping
from pathlib import Path


from .publication_state_projection import PublicationReconciliationError


def sha256_file(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublicationReconciliationError(
            f"{label} unreadable: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PublicationReconciliationError(f"{label} is not an object")
    return value


def _validate_sha_entry(value: object, label: str) -> Path:
    if not isinstance(value, Mapping):
        raise PublicationReconciliationError(f"{label} binding is missing")
    path = Path(str(value.get("path") or ""))
    if not path.is_file():
        raise PublicationReconciliationError(f"{label} missing: {path}")
    actual_sha = sha256_file(path)
    actual_bytes = path.stat().st_size
    if value.get("sha256") != actual_sha or value.get("bytes") != actual_bytes:
        raise PublicationReconciliationError(f"{label} hash/bytes drifted")
    return path.resolve()


