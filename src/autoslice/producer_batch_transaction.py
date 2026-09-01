"""Lane-specific state-last commit for privately prepared Talk/Song packages.

This deliberately serves only the ordinary producer lanes.  It binds an
ordered prepared prefix, exact runner-state bytes, and the deployed seal into
one small journal; it is not a reusable whole-state transition framework.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.producer_delivery_transaction import (
    PreparedDelivery,
    ProducerDeliveryTransactionError,
    _canonical_bytes,
    _fsync_directory,
    _read_document,
    _private_directory,
    _read_regular_bytes,
    _write_create_only,
    _write_replace,
    materialize_preflight_under_batch,
    preflight_prepared_delivery,
)
from src.autoslice.qixi_transaction_core import (
    RunnerCommitLease,
    require_runner_commit_lease,
)
from src.autoslice.runner_state_writeback import (
    RunnerStateWritebackError,
    read_exact_state_preimage,
    state_bytes,
    write_exact_state_under_lease,
)


PRODUCER_BATCH_JOURNAL_SCHEMA = "producer-batch-journal.v1"


class ProducerBatchTransactionError(RuntimeError):
    """A prepared producer prefix cannot be installed safely."""


@dataclass(frozen=True, slots=True)
class PreparedBatchEntry:
    """One ordered candidate result and its private delivery capability."""

    lane: str
    candidate_id: str
    handle: PreparedDelivery


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _b64(payload: bytes | None) -> str | None:
    return None if payload is None else base64.b64encode(payload).decode("ascii")


def _unb64(value: object, *, label: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProducerBatchTransactionError(f"batch journal {label} is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProducerBatchTransactionError(f"batch journal {label} is invalid") from exc


def _current(path: Path, *, runtime_root: Path) -> bytes | None:
    try:
        return read_exact_state_preimage(path, runtime_root=runtime_root)
    except RunnerStateWritebackError as exc:
        raise ProducerBatchTransactionError("batch state path is unsafe") from exc


def _journal_path(runtime_root: Path, *, date: str, digest: str) -> Path:
    if not isinstance(date, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
        raise ProducerBatchTransactionError("batch date is unsafe")
    return runtime_root / ".producer-batch-journal" / f"{date}-{digest}.json"


def _journal_digest(*, date: str, state_path: Path, before: bytes | None, after: bytes, entries: Sequence[PreparedBatchEntry]) -> str:
    seed = {
        "schema_version": PRODUCER_BATCH_JOURNAL_SCHEMA,
        "date": date,
        "state_path": str(state_path),
        "state_before_sha256": _sha(before or b""),
        "state_before_missing": before is None,
        "state_after_sha256": _sha(after),
        "entries": [
            {"lane": entry.lane, "candidate_id": entry.candidate_id,
             "manifest_path": str(entry.handle.manifest_path),
             "prepared_sha256": f"sha256:{entry.handle.prepared_sha256}"}
            for entry in entries
        ],
    }
    return hashlib.sha256(_canonical_bytes(seed)).hexdigest()


def _journal_document(
    *, date: str, state_path: Path, before: bytes | None, after: bytes,
    entries: Sequence[PreparedBatchEntry], inventory: Sequence[str], status: str,
    installed: Sequence[str],
) -> dict:
    document = {
        "schema_version": PRODUCER_BATCH_JOURNAL_SCHEMA,
        "date": date,
        "state_path": str(state_path),
        "state_before_b64": _b64(before),
        "state_before_sha256": _sha(before or b""),
        "state_before_missing": before is None,
        "state_after_b64": _b64(after),
        "state_after_sha256": _sha(after),
        "entries": [
            {"lane": entry.lane, "candidate_id": entry.candidate_id,
             "manifest_path": str(entry.handle.manifest_path),
             "prepared_sha256": f"sha256:{entry.handle.prepared_sha256}"}
            for entry in entries
        ],
        "status": status,
        "artifact_inventory": list(inventory),
        "installed_artifacts": list(installed),
        "upload_enabled": False,
    }
    document["journal_sha256"] = _sha(_canonical_bytes(document))
    return document


def _read_journal(path: Path) -> dict:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise ProducerBatchTransactionError("batch journal is unavailable") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o600:
        raise ProducerBatchTransactionError("batch journal is unsafe")
    try:
        document = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ProducerDeliveryTransactionError) as exc:
        raise ProducerBatchTransactionError("batch journal is invalid") from exc
    if not isinstance(document, dict):
        raise ProducerBatchTransactionError("batch journal is invalid")
    required = {
        "schema_version", "date", "state_path", "state_before_b64",
        "state_before_sha256", "state_before_missing", "state_after_b64",
        "state_after_sha256", "entries", "artifact_inventory", "status", "installed_artifacts",
        "upload_enabled", "journal_sha256",
    }
    unsigned = dict(document)
    declared = unsigned.pop("journal_sha256", None)
    if (
        set(document) != required
        or document.get("schema_version") != PRODUCER_BATCH_JOURNAL_SCHEMA
        or document.get("status") not in {"PREPARED", "INSTALLING", "COMMITTED"}
        or document.get("upload_enabled") is not False
        or not isinstance(declared, str)
        or declared != _sha(_canonical_bytes(unsigned))
        or not isinstance(document.get("entries"), list)
        or not document["entries"]
        or not isinstance(document.get("artifact_inventory"), list)
        or not isinstance(document.get("installed_artifacts"), list)
    ):
        raise ProducerBatchTransactionError("batch journal authority drifts")
    before = _unb64(document["state_before_b64"], label="state before")
    after = _unb64(document["state_after_b64"], label="state after")
    if (
        (before is None) != bool(document["state_before_missing"])
        or _sha(before or b"") != document["state_before_sha256"]
        or after is None
        or _sha(after) != document["state_after_sha256"]
    ):
        raise ProducerBatchTransactionError("batch journal state binding drifts")
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "lane", "candidate_id", "manifest_path", "prepared_sha256",
        } or not all(isinstance(entry[key], str) and entry[key] for key in entry):
            raise ProducerBatchTransactionError("batch journal entry drifts")
    inventory = document["artifact_inventory"]
    installed = document["installed_artifacts"]
    if (
        not inventory
        or any(not isinstance(value, str) or not value for value in inventory)
        or any(not isinstance(value, str) for value in installed)
        or installed != inventory[:len(installed)]
        or (document["status"] == "PREPARED" and installed)
        or (document["status"] == "INSTALLING" and not installed)
        or (document["status"] == "COMMITTED" and installed != inventory)
    ):
        raise ProducerBatchTransactionError("batch journal checkpoint drifts")
    return document


def _artifact_inventory(entries: Sequence[PreparedBatchEntry], preflight: Sequence[object]) -> list[str]:
    inventory: list[str] = []
    for entry, checked in zip(entries, preflight, strict=True):
        for artifact, _staged, target, expected in checked.checked:
            inventory.append(
                f"{entry.candidate_id}:{artifact['role']}:{target}:{expected}"
            )
    return inventory


def _entries_from_journal(runtime_root: Path, document: Mapping[str, object]) -> list[PreparedBatchEntry]:
    entries: list[PreparedBatchEntry] = []
    for value in document["entries"]:
        assert isinstance(value, dict)
        prepared = str(value["prepared_sha256"])
        if not prepared.startswith("sha256:") or len(prepared) != 71:
            raise ProducerBatchTransactionError("batch prepared digest drifts")
        entries.append(PreparedBatchEntry(
            lane=str(value["lane"]), candidate_id=str(value["candidate_id"]),
            handle=PreparedDelivery(runtime_root, str(value["lane"]), str(value["candidate_id"]),
                                    prepared.removeprefix("sha256:"), Path(str(value["manifest_path"]))),
        ))
    if len({entry.candidate_id for entry in entries}) != len(entries):
        raise ProducerBatchTransactionError("batch journal candidate inventory drifts")
    return entries


def _validate_journal_binding(*, runtime_root: Path, path: Path, document: Mapping[str, object]) -> None:
    date = document.get("date")
    if not isinstance(date, str):
        raise ProducerBatchTransactionError("batch journal date drifts")
    expected_state = runtime_root / "state" / f"{date}.json"
    if document.get("state_path") != str(expected_state):
        raise ProducerBatchTransactionError("batch journal state path drifts")
    before = _unb64(document.get("state_before_b64"), label="state before")
    after = _unb64(document.get("state_after_b64"), label="state after")
    if after is None:
        raise ProducerBatchTransactionError("batch journal state binding drifts")
    entries = _entries_from_journal(runtime_root, document)
    digest = _journal_digest(
        date=date, state_path=expected_state, before=before, after=after, entries=entries,
    )
    if path != _journal_path(runtime_root, date=date, digest=digest):
        raise ProducerBatchTransactionError("batch journal filename drifts")


def validate_committed_batch_journal_for_deploy(*, runtime_root: Path, path: Path) -> None:
    """Read-only deploy gate for a completed ordinary-producer receipt.

    Deployment cannot cross an unfinished after-image.  A bare ``status`` is
    not authority: bind the receipt filename, every private prepared manifest,
    and the exact final checkpoint inventory before accepting COMMITTED.
    """

    document = _read_journal(path)
    _validate_journal_binding(runtime_root=runtime_root, path=path, document=document)
    if document.get("status") != "COMMITTED":
        raise ProducerBatchTransactionError("producer batch journal is pending")
    entries = _entries_from_journal(runtime_root, document)
    inventory: list[str] = []
    targets: set[str] = set()
    for entry in entries:
        prepared = _read_document(entry.handle, allow_materialized=True)
        for artifact in prepared["artifacts"]:
            assert isinstance(artifact, dict)
            target = str(artifact["target_path"])
            if target in targets:
                raise ProducerBatchTransactionError("producer batch journal targets collide")
            targets.add(target)
            inventory.append(
                f"{entry.candidate_id}:{artifact['role']}:{target}:"
                f"{str(artifact['staged_sha256']).removeprefix('sha256:')}"
            )
    if document.get("artifact_inventory") != inventory or document.get("installed_artifacts") != inventory:
        raise ProducerBatchTransactionError("producer batch committed inventory drifts")


def commit_prepared_prefix(
    *, runtime_root: Path, date: str, state_path: Path, state_before: bytes | None,
    after_state: Mapping[str, object], entries: Sequence[PreparedBatchEntry],
    lease: RunnerCommitLease, journal_path: Path | None = None,
) -> dict:
    """Commit an ordered prepared prefix, targets first and exact state last."""

    require_runner_commit_lease(lease, runtime_root=runtime_root)
    if not entries:
        raise ProducerBatchTransactionError("prepared producer prefix is empty")
    if (
        len({entry.candidate_id for entry in entries}) != len(entries)
        or any(
            entry.lane not in {"talk", "song"}
            or not entry.candidate_id
            or entry.handle.runtime_root != runtime_root
            or entry.handle.lane != entry.lane
            or entry.handle.candidate_id != entry.candidate_id
            for entry in entries
        )
    ):
        raise ProducerBatchTransactionError("prepared producer prefix inventory is unsafe")
    if state_path != runtime_root / "state" / f"{date}.json":
        raise ProducerBatchTransactionError("batch state path drifts")
    after = state_bytes(after_state)
    digest = _journal_digest(date=date, state_path=state_path, before=state_before, after=after, entries=entries)
    path = journal_path or _journal_path(runtime_root, date=date, digest=digest)
    if path != _journal_path(runtime_root, date=date, digest=digest):
        raise ProducerBatchTransactionError("batch journal path drifts")
    current = _current(state_path, runtime_root=runtime_root)
    if current not in {state_before, after}:
        raise ProducerBatchTransactionError("batch state preimage drifts")

    if path.exists() or path.is_symlink():
        journal = _read_journal(path)
        _validate_journal_binding(runtime_root=runtime_root, path=path, document=journal)
        if _entries_from_journal(runtime_root, journal) != list(entries):
            raise ProducerBatchTransactionError("batch journal inventory drifts")
        if (
            journal["date"] != date
            or journal["state_path"] != str(state_path)
            or _unb64(journal["state_before_b64"], label="state before") != state_before
            or _unb64(journal["state_after_b64"], label="state after") != after
        ):
            raise ProducerBatchTransactionError("batch journal caller binding drifts")
    else:
        # All handles and all target/source/staged preimages are checked before
        # the first formal batch-journal inode exists.
        try:
            preflight = [
                preflight_prepared_delivery(handle=entry.handle, lease=lease)
                for entry in entries
            ]
        except ProducerDeliveryTransactionError as exc:
            raise ProducerBatchTransactionError(str(exc)) from exc
        targets = [target for checked in preflight for _artifact, _staged, target, _sha in checked.checked]
        if len(set(targets)) != len(targets):
            raise ProducerBatchTransactionError("prepared producer targets collide")
        current = _current(state_path, runtime_root=runtime_root)
        if current != state_before:
            raise ProducerBatchTransactionError("batch state preimage drifts")
        _private_directory(runtime_root, ".producer-batch-journal")
        inventory = _artifact_inventory(entries, preflight)
        journal = _journal_document(
            date=date, state_path=state_path, before=state_before, after=after,
            entries=entries, inventory=inventory, status="PREPARED", installed=[],
        )
        _write_create_only(path, _canonical_bytes(journal))
        _fsync_directory(path.parent)
        installed: list[str] = []
        for entry, checked in zip(entries, preflight, strict=True):
            def checkpoint(artifact: dict[str, str], *, current_entry: PreparedBatchEntry = entry) -> None:
                installed.append(
                    f"{current_entry.candidate_id}:{artifact['role']}:{artifact['path']}:"
                    f"{artifact['sha256'].removeprefix('sha256:')}"
                )
                replacement = _journal_document(
                    date=date, state_path=state_path, before=state_before, after=after,
                    entries=entries, inventory=inventory, status="INSTALLING", installed=installed,
                )
                _write_replace(path, _canonical_bytes(replacement))
                _fsync_directory(path.parent)
            materialize_preflight_under_batch(checked, lease=lease, checkpoint=checkpoint)
        if current != after:
            try:
                write_exact_state_under_lease(
                    state_path, runtime_root=runtime_root, lease=lease,
                    expected_before=state_before, after=after_state,
                )
            except RunnerStateWritebackError as exc:
                raise ProducerBatchTransactionError(str(exc)) from exc
        journal = _journal_document(
            date=date, state_path=state_path, before=state_before, after=after,
            entries=entries, inventory=inventory, status="COMMITTED", installed=installed,
        )
        _write_replace(path, _canonical_bytes(journal))
        _fsync_directory(path.parent)
        return {"journal_path": str(path), "installed_artifacts": installed}

    # Existing journal recovery is deliberately provider-free.  Revalidate
    # owned prepared sources/targets under this lease, then replay state-last.
    journal_entries = _entries_from_journal(runtime_root, journal)
    try:
        preflight = [
            preflight_prepared_delivery(handle=entry.handle, lease=lease, allow_installed=True)
            for entry in journal_entries
        ]
    except ProducerDeliveryTransactionError as exc:
        raise ProducerBatchTransactionError(str(exc)) from exc
    inventory = _artifact_inventory(journal_entries, preflight)
    if journal["artifact_inventory"] != inventory:
        raise ProducerBatchTransactionError("batch journal inventory drifts")
    installed = list(journal["installed_artifacts"])
    for entry, checked in zip(journal_entries, preflight, strict=True):
        def checkpoint(artifact: dict[str, str], *, current_entry: PreparedBatchEntry = entry) -> None:
            key = (
                f"{current_entry.candidate_id}:{artifact['role']}:{artifact['path']}:"
                f"{artifact['sha256'].removeprefix('sha256:')}"
            )
            if key not in installed:
                installed.append(key)
                replacement = _journal_document(
                    date=date, state_path=state_path, before=state_before, after=after,
                    entries=journal_entries, inventory=inventory,
                    status="INSTALLING", installed=installed,
                )
                _write_replace(path, _canonical_bytes(replacement))
                _fsync_directory(path.parent)
        materialize_preflight_under_batch(checked, lease=lease, checkpoint=checkpoint)
    if _current(state_path, runtime_root=runtime_root) != after:
        try:
            parsed_after = json.loads(after.decode("utf-8"))
            if not isinstance(parsed_after, dict):
                raise ValueError("not object")
            write_exact_state_under_lease(
                state_path, runtime_root=runtime_root, lease=lease,
                expected_before=state_before, after=parsed_after,
            )
        except (UnicodeDecodeError, ValueError, RunnerStateWritebackError) as exc:
            raise ProducerBatchTransactionError("batch state recovery drifts") from exc
    journal = _journal_document(
        date=date, state_path=state_path, before=state_before, after=after,
        entries=journal_entries, inventory=inventory, status="COMMITTED", installed=installed,
    )
    _write_replace(path, _canonical_bytes(journal))
    _fsync_directory(path.parent)
    return {"journal_path": str(path), "installed_artifacts": installed}


def resume_pending_batch(*, runtime_root: Path, date: str, lease: RunnerCommitLease) -> dict | None:
    """Replay exactly one valid batch journal for a date before provider work."""

    require_runner_commit_lease(lease, runtime_root=runtime_root)
    root = runtime_root / ".producer-batch-journal"
    if not root.exists() and not root.is_symlink():
        return None
    try:
        observed = os.lstat(root)
    except OSError as exc:
        raise ProducerBatchTransactionError("producer batch journal namespace is unsafe") from exc
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise ProducerBatchTransactionError("producer batch journal namespace is unsafe")
    candidates = sorted(root.glob(f"{date}-*.json"))
    if not candidates:
        return None
    pending: list[tuple[Path, dict]] = []
    for candidate in candidates:
        journal = _read_journal(candidate)
        _validate_journal_binding(runtime_root=runtime_root, path=candidate, document=journal)
        if journal["date"] != date:
            raise ProducerBatchTransactionError("producer batch journal date drifts")
        if journal["status"] != "COMMITTED":
            pending.append((candidate, journal))
    if not pending:
        return None
    if len(pending) != 1:
        raise ProducerBatchTransactionError("producer batch journal namespace is ambiguous")
    journal_path, journal = pending[0]
    before = _unb64(journal["state_before_b64"], label="state before")
    after = _unb64(journal["state_after_b64"], label="state after")
    assert after is not None
    parsed_after = json.loads(after.decode("utf-8"))
    if not isinstance(parsed_after, dict):
        raise ProducerBatchTransactionError("producer batch state after-image is invalid")
    return commit_prepared_prefix(
        runtime_root=runtime_root, date=date, state_path=Path(str(journal["state_path"])),
        state_before=before, after_state=parsed_after,
        entries=_entries_from_journal(runtime_root, journal), lease=lease,
        journal_path=journal_path,
    )
