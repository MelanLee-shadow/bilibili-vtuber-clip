#!/usr/bin/env python3
"""Dry-run first, sealed v7 selected-final-review recovery materializer.

It has one allowed state transition: an unchanged selected final-review
rejection becomes the standard receipt-bound Talk queue row.  It does not
invoke a provider, package builder, upload, or publication API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.session_autoslice as runner  # noqa: E402
from src.autoslice.qixi_transaction_core import exclusive_runner_commit  # noqa: E402
from src.autoslice.runner_state_writeback import (  # noqa: E402
    read_exact_state_preimage,
    state_bytes,
    write_exact_state_under_lease,
)
from src.autoslice.selected_final_review_recovery import (  # noqa: E402
    RECOVERY_RECEIPT_FIELD,
    validate_selected_final_review_recovery_receipt,
)
from src.autoslice.selected_final_review_recovery_authority import (  # noqa: E402
    SelectedFinalReviewRecoveryAuthorityError,
    build_authorized_selected_final_review_recovery_receipt,
    load_selected_final_review_recovery_authority,
    materialize_selected_final_review_recovery_scope,
    validate_selected_final_review_scope_predecessor,
    validate_selected_final_review_recovery_rejection,
)


_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SIDECAR_SCHEMA = "selected-final-review-recovery-materialization.v1"
_SIDECAR_STATUS = "PREPARED"
_SIDECAR_INTENT = "MATERIALIZE_SELECTED_FINAL_REVIEW_RECOVERY"
_SIDECAR_FIELDS = frozenset(
    {
        "schema_version", "status", "intent", "candidate_id", "recording_date",
        "deployed_commit", "authority_sha256", "state_before_sha256",
        "state_after_sha256", "scope_sha256", "recovery_receipt_sha256",
        "upload_allowed", "provider_called",
    }
)


class SelectedFinalReviewRecoveryMaterializationError(RuntimeError):
    """The one permitted recovery transition cannot safely be committed."""


@dataclass(frozen=True, slots=True)
class PreparedRecovery:
    authority: dict[str, object]
    deployed_commit: str
    state_path: Path
    before_bytes: bytes | None
    state_before_sha256: str
    after_state: dict[str, object]
    receipt: dict[str, object]
    sidecar: Path


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_deployed_commit(repo_root: Path) -> str:
    path = repo_root / "DEPLOYED_COMMIT"
    if path.is_symlink() or not path.is_file():
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_DEPLOYED_IDENTITY_UNAVAILABLE"
        )
    try:
        value = path.read_text(encoding="utf-8").split(maxsplit=1)[0]
    except (OSError, UnicodeError) as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_DEPLOYED_IDENTITY_UNAVAILABLE"
        ) from exc
    if _COMMIT_RE.fullmatch(value) is None:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_DEPLOYED_IDENTITY_INVALID"
        )
    return value


def _parse_state(payload: bytes) -> dict[str, object]:
    try:
        state = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_STATE_INVALID"
        ) from exc
    if not isinstance(state, dict):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_STATE_INVALID"
        )
    return state


def _candidate_queue_row(state: Mapping[str, object], candidate_id: str) -> dict[str, object]:
    rows = [
        row
        for row in state.get("pending_talk", [])
        if isinstance(row, dict)
        and str(row.get("candidate_id") or row.get("cid") or "") == candidate_id
    ] if isinstance(state.get("pending_talk"), list) else []
    if len(rows) != 1:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_QUEUE_NOT_UNIQUE"
        )
    return rows[0]


def _sidecar_path(*, runtime_root: Path, date: str, candidate_id: str) -> Path:
    return runtime_root / "state" / f"{date}.{candidate_id}.selected-final-review-recovery.json"


def _sidecar_document(prepared: PreparedRecovery) -> dict[str, object]:
    return {
        "schema_version": _SIDECAR_SCHEMA,
        "status": _SIDECAR_STATUS,
        "intent": _SIDECAR_INTENT,
        "candidate_id": prepared.authority["candidate_id"],
        "recording_date": prepared.authority["state_date"],
        "deployed_commit": prepared.deployed_commit,
        "authority_sha256": prepared.authority["authority_sha256"],
        "state_before_sha256": prepared.state_before_sha256,
        "state_after_sha256": _sha256(state_bytes(prepared.after_state)),
        "scope_sha256": _canonical_json_sha256(prepared.after_state["operator_processing_scope"]),
        "recovery_receipt_sha256": prepared.receipt["receipt_sha256"],
        "upload_allowed": False,
        "provider_called": False,
    }


def _canonical_json_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_sidecar(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_INVALID"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_INVALID"
        ) from exc
    if not isinstance(value, dict) or set(value) != _SIDECAR_FIELDS:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_INVALID"
        )
    if value.get("schema_version") != _SIDECAR_SCHEMA or value.get("status") != _SIDECAR_STATUS or value.get("intent") != _SIDECAR_INTENT:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_INVALID"
        )
    if not (
        isinstance(value.get("candidate_id"), str)
        and isinstance(value.get("recording_date"), str)
        and _COMMIT_RE.fullmatch(str(value.get("deployed_commit")))
        and all(
            isinstance(value.get(field), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value[field]))
            for field in (
                "authority_sha256", "state_before_sha256", "state_after_sha256",
                "scope_sha256", "recovery_receipt_sha256",
            )
        )
        and value.get("upload_allowed") is False
        and value.get("provider_called") is False
    ):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_INVALID"
        )
    return value


def _create_sidecar(path: Path, document: Mapping[str, object]) -> None:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    if path.parent.is_symlink():
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_PARENT_UNSAFE"
        )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_EXISTS"
        ) from exc
    except OSError as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_CREATE_FAILED"
        ) from exc
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_prepared_sidecar(
    document: Mapping[str, object], *, prepared: PreparedRecovery
) -> None:
    if dict(document) != _sidecar_document(prepared):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_MISMATCH"
        )


def _resume_committed_sidecar(
    *,
    document: Mapping[str, object],
    repo_root: Path,
    state_path: Path,
    candidate_id: str,
    expected_deployed_commit: str,
) -> PreparedRecovery:
    """Verify an already-CASed prepared journal without recreating its old row."""

    authority, _seal = load_selected_final_review_recovery_authority(
        repo_root=repo_root, candidate_id=candidate_id
    )
    deployed_commit = _read_deployed_commit(repo_root)
    current = read_exact_state_preimage(state_path, runtime_root=state_path.parent.parent)
    if current is None or _sha256(current) != document.get("state_after_sha256"):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_STATE_DRIFT"
        )
    after = _parse_state(current)
    scope = after.get("operator_processing_scope")
    if not (
        document.get("candidate_id") == candidate_id
        and document.get("recording_date") == state_path.stem
        and document.get("deployed_commit") == expected_deployed_commit == deployed_commit
        and document.get("authority_sha256") == authority.get("authority_sha256")
        and document.get("scope_sha256") == _canonical_json_sha256(scope)
        and document.get("upload_allowed") is False
        and document.get("provider_called") is False
    ):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_MISMATCH"
        )
    queue = _candidate_queue_row(after, candidate_id)
    receipt = queue.get(RECOVERY_RECEIPT_FIELD)
    if not isinstance(receipt, dict) or receipt.get("receipt_sha256") != document.get("recovery_receipt_sha256"):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_MISMATCH"
        )
    if not validate_selected_final_review_recovery_receipt(
        receipt,
        queued_row=queue,
        candidate_id=candidate_id,
        grant_id=str(scope.get("grant_id")) if isinstance(scope, Mapping) else "",
        current_fingerprint=str(receipt.get("current_failure_recovery_fingerprint") or ""),
    ):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_MISMATCH"
        )
    return PreparedRecovery(
        authority=authority,
        deployed_commit=deployed_commit,
        state_path=state_path,
        before_bytes=None,
        state_before_sha256=str(document["state_before_sha256"]),
        after_state=after,
        receipt=receipt,
        sidecar=_sidecar_path(runtime_root=state_path.parent.parent, date=state_path.stem, candidate_id=candidate_id),
    )


def prepare_recovery(
    *,
    repo_root: Path,
    runtime_root: Path,
    state_path: Path,
    candidate_id: str,
    expected_deployed_commit: str,
    expires_at: str,
    runtime: ModuleType = runner,
) -> PreparedRecovery:
    """Read and fully validate the exact transition without writing anything."""

    runtime_root = runtime_root.resolve(strict=True)
    state_path = state_path.resolve(strict=True)
    if (
        state_path.parent != runtime_root / "state"
        or _DATE_RE.fullmatch(state_path.stem) is None
        or _COMMIT_RE.fullmatch(expected_deployed_commit) is None
    ):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_PATH_OR_COMMIT_INVALID"
        )
    deployed_commit = _read_deployed_commit(repo_root)
    if deployed_commit != expected_deployed_commit:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_DEPLOYED_COMMIT_DRIFT"
        )
    try:
        authority, _seal = load_selected_final_review_recovery_authority(
            repo_root=repo_root, candidate_id=candidate_id
        )
    except SelectedFinalReviewRecoveryAuthorityError as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_AUTHORITY_INVALID"
        ) from exc
    date = state_path.stem
    if authority.get("state_date") != date:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_STATE_DATE_DRIFT"
        )
    before = read_exact_state_preimage(state_path, runtime_root=runtime_root)
    if before is None:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_STATE_UNAVAILABLE"
        )
    state = _parse_state(before)
    try:
        old_row = validate_selected_final_review_recovery_rejection(
            authority, state=state, state_date=date
        )
    except SelectedFinalReviewRecoveryAuthorityError as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_REJECTION_DRIFT"
        ) from exc
    try:
        scope = materialize_selected_final_review_recovery_scope(
            authority, expires_at=expires_at
        )
        validate_selected_final_review_scope_predecessor(
            authority, repo_root=repo_root, state=state, scope=scope
        )
    except SelectedFinalReviewRecoveryAuthorityError as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_SCOPE_PREDECESSOR_CONFLICT"
        ) from exc
    after = deepcopy(state)
    after["operator_processing_scope"] = scope
    current_fingerprint = runtime.talk_failure_recovery_fingerprint(
        "subtitle_authority", candidate_id
    )
    if current_fingerprint == old_row.get("failure_recovery_fingerprint"):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_FINGERPRINT_UNCHANGED"
        )
    count = runtime.requeue_recoverable_talks(
        date, after, candidate_ids=(candidate_id,)
    )
    if count != 1:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_STANDARD_TRANSITION_BLOCKED"
        )
    queue = _candidate_queue_row(after, candidate_id)
    queue_body = {
        key: value for key, value in queue.items() if key != RECOVERY_RECEIPT_FIELD
    }
    try:
        receipt = build_authorized_selected_final_review_recovery_receipt(
            authority,
            state={**state, "operator_processing_scope": scope},
            state_date=date,
            queued_row=queue_body,
            current_fingerprint=current_fingerprint,
        )
    except SelectedFinalReviewRecoveryAuthorityError as exc:
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_BINDING_INVALID"
        ) from exc
    if not (
        queue.get(RECOVERY_RECEIPT_FIELD) == receipt
        and validate_selected_final_review_recovery_receipt(
            receipt,
            queued_row=queue,
            candidate_id=candidate_id,
            grant_id=str(scope["grant_id"]),
            current_fingerprint=current_fingerprint,
        )
    ):
        raise SelectedFinalReviewRecoveryMaterializationError(
            "SELECTED_FINAL_REVIEW_RECOVERY_STANDARD_RECEIPT_DRIFT"
        )
    return PreparedRecovery(
        authority=authority,
        deployed_commit=deployed_commit,
        state_path=state_path,
        before_bytes=before,
        state_before_sha256=_sha256(before),
        after_state=after,
        receipt=receipt,
        sidecar=_sidecar_path(runtime_root=runtime_root, date=date, candidate_id=candidate_id),
    )


def apply_recovery(**kwargs: object) -> PreparedRecovery:
    """Re-run preflight under one lock, create the receipt, and exact-CAS state."""

    runtime_root = Path(str(kwargs["runtime_root"]))
    with exclusive_runner_commit(runtime_root) as lease:
        runtime_root = runtime_root.resolve(strict=True)
        state_path = Path(str(kwargs["state_path"])).resolve(strict=True)
        if state_path.parent != runtime_root / "state" or _DATE_RE.fullmatch(state_path.stem) is None:
            raise SelectedFinalReviewRecoveryMaterializationError(
                "SELECTED_FINAL_REVIEW_RECOVERY_PATH_OR_COMMIT_INVALID"
            )
        candidate_id = str(kwargs["candidate_id"])
        sidecar = _sidecar_path(runtime_root=runtime_root, date=state_path.stem, candidate_id=candidate_id)
        existing = _read_sidecar(sidecar)
        current = read_exact_state_preimage(state_path, runtime_root=runtime_root)
        if existing is not None:
            if current is not None and _sha256(current) == existing.get("state_after_sha256"):
                return _resume_committed_sidecar(
                    document=existing,
                    repo_root=Path(str(kwargs["repo_root"])),
                    state_path=state_path,
                    candidate_id=candidate_id,
                    expected_deployed_commit=str(kwargs["expected_deployed_commit"]),
                )
            if current is None or _sha256(current) != existing.get("state_before_sha256"):
                raise SelectedFinalReviewRecoveryMaterializationError(
                    "SELECTED_FINAL_REVIEW_RECOVERY_SIDECAR_STATE_DRIFT"
                )
        prepared = prepare_recovery(**kwargs)  # type: ignore[arg-type]
        if existing is None:
            _create_sidecar(prepared.sidecar, _sidecar_document(prepared))
        else:
            _validate_prepared_sidecar(existing, prepared=prepared)
        try:
            write_exact_state_under_lease(
                prepared.state_path,
                runtime_root=runtime_root,
                lease=lease,
                expected_before=prepared.before_bytes,
                after=prepared.after_state,
            )
        except Exception as exc:
            raise SelectedFinalReviewRecoveryMaterializationError(
                "SELECTED_FINAL_REVIEW_RECOVERY_STATE_CAS_FAILED"
            ) from exc
        if read_exact_state_preimage(prepared.state_path, runtime_root=runtime_root) != state_bytes(prepared.after_state):
            raise SelectedFinalReviewRecoveryMaterializationError(
                "SELECTED_FINAL_REVIEW_RECOVERY_STATE_READBACK_DRIFT"
            )
    return prepared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--expected-deployed-commit", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    values = {
        "repo_root": args.repo_root,
        "runtime_root": args.runtime_root,
        "state_path": args.state,
        "candidate_id": args.candidate_id,
        "expected_deployed_commit": args.expected_deployed_commit,
        "expires_at": args.expires_at,
    }
    try:
        prepared = apply_recovery(**values) if args.apply else prepare_recovery(**values)
    except SelectedFinalReviewRecoveryMaterializationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "APPLIED" if args.apply else "DRY_RUN_READY",
        "candidate_id": prepared.authority["candidate_id"],
        "state_before_sha256": prepared.state_before_sha256,
        "state_after_sha256": _sha256(state_bytes(prepared.after_state)),
        "receipt_sha256": prepared.receipt["receipt_sha256"],
        "provider_called": False,
        "upload_allowed": False,
        "sidecar": str(prepared.sidecar) if args.apply else None,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
