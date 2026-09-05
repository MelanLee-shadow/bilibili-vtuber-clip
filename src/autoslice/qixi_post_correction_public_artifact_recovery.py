"""Fixed recovery for the one superseded hidden Qixi public-artifact namespace.

This is intentionally not a generic relocation tool.  It consumes only the
already-COMMITTED public-surface journal for ``auto_123655_771_844`` and moves
only its create-only cover sidecars to the successor safe-basename namespace.
The original journal remains the authority for the already-produced cover
bytes; this recovery cannot render, call a provider, or change media/text.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from src.autoslice import qixi_post_correction_public_surface as surface
from src.autoslice.qixi_post_correction_projection_paths import (
    QixiPostCorrectionPublicSurfaceError,
    canonical_sha256,
    create_staged_file,
    clear_staged_file_owner,
    legacy_public_artifact_root,
    public_artifact_path,
    public_artifact_root,
    sha256_bytes,
)
from src.autoslice.qixi_post_correction_sealed_replay import sealed_chat_authority_replay


SCHEMA = "qixi-post-correction-public-artifact-basename-recovery.v1"
RECEIPT_SCHEMA = "qixi-post-correction-public-artifact-basename-recovery-receipt.v1"
_ROOT_NAME = "qixi_post_correction_public_artifact_basename_recovery"
_ENTRY_KEYS = frozenset(
    {
        "role", "target", "before_bytes_b64", "before_sha256", "before_mode",
        "before_device", "before_inode", "after_bytes_b64", "after_sha256",
        "after_mode", "phase", "staged_name", "staged_device", "staged_inode",
        "installed_device", "installed_inode", "backup_name",
    }
)
_JOURNAL_KEYS = frozenset(
    {
        "schema_version", "status", "candidate_id", "recording_date", "upload_enabled",
        "authority_sha256", "source_journal_sha256", "source_final_receipt_sha256",
        "entries", "path_mapping", "journal_sha256",
    }
)


def _error(message: str) -> QixiPostCorrectionPublicSurfaceError:
    return QixiPostCorrectionPublicSurfaceError(message)


def _b64(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _unb64(value: object, *, label: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(f"basename recovery {label} is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError as exc:
        raise _error(f"basename recovery {label} is invalid") from exc


def _digest(payload: bytes) -> str:
    return sha256_bytes(payload)


def _journal_digest(value: Mapping[str, object]) -> str:
    unsigned = dict(value)
    unsigned.pop("journal_sha256", None)
    return canonical_sha256(unsigned)


def _recovery_root(authority: Mapping[str, object]) -> Path:
    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    record = Path(str(artifacts["record"]["path"]))
    return record.parent.parent / _ROOT_NAME / str(authority["authority_sha256"])[7:23]


def _safe_root(root: Path, *, create: bool) -> None:
    surface._private_journal_root(root, create=create)


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    surface._require_regular(path, label=label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _error(f"basename recovery {label} is unreadable") from exc
    if not isinstance(value, dict):
        raise _error(f"basename recovery {label} schema drifts")
    return value


def _source_journal(
    authority: Mapping[str, object],
    *,
    require_live_after_image: bool,
    sealed_chat_authority_bytes: bytes | None = None,
) -> tuple[Path, dict[str, object], bytes]:
    root = surface._journal_root_from_authority(authority)
    journal_path = root / "journal.json"
    if not os.path.lexists(root):
        raise _error("basename recovery requires the committed public-surface journal")
    surface._private_journal_root(root, create=False)
    with sealed_chat_authority_replay(sealed_chat_authority_bytes):
        journal = surface._validate_journal(
            _read_json(journal_path, label="source journal"), authority=authority
        )
    if journal.get("status") != "COMMITTED":
        raise _error("basename recovery requires the committed public-surface journal")
    receipt = root / "final-receipt.json"
    surface._require_regular(receipt, label="post-correction final receipt")
    expected = surface._json_bytes(surface._final_receipt(journal))
    observed = receipt.read_bytes()
    if observed != expected:
        raise _error("basename recovery source final receipt drifts")
    if require_live_after_image:
        surface._postcommit_replay(journal, authority=authority)
    return root, journal, observed


def _decode_legacy_relative(path: Path, *, legacy_namespace: Path) -> str:
    if path.parent != legacy_namespace.parent or not path.name.startswith(legacy_namespace.name):
        raise _error("basename recovery legacy artifact path drifts")
    encoded = path.name.removeprefix(legacy_namespace.name)
    if not encoded or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in encoded):
        raise _error("basename recovery legacy artifact encoding drifts")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        relative = raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise _error("basename recovery legacy artifact encoding drifts") from exc
    # Re-encoding gives a single canonical, injective name representation.
    if public_artifact_path(legacy_namespace, relative) != path:
        raise _error("basename recovery legacy artifact encoding drifts")
    return relative


def _replace_exact(value: object, mapping: Mapping[str, str]) -> object:
    if isinstance(value, Mapping):
        return {key: _replace_exact(child, mapping) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_exact(child, mapping) for child in value]
    return mapping.get(value, value) if isinstance(value, str) else value


def _assert_only_mapped_strings(before: object, after: object, mapping: Mapping[str, str]) -> None:
    if isinstance(before, Mapping):
        if not isinstance(after, Mapping) or set(before) != set(after):
            raise _error("basename recovery changes document structure")
        for key in before:
            _assert_only_mapped_strings(before[key], after[key], mapping)
        return
    if isinstance(before, list):
        if not isinstance(after, list) or len(before) != len(after):
            raise _error("basename recovery changes document structure")
        for old, new in zip(before, after, strict=True):
            _assert_only_mapped_strings(old, new, mapping)
        return
    expected = mapping.get(before, before) if isinstance(before, str) else before
    if after != expected:
        raise _error("basename recovery changes an unrelated field")


def _entry_snapshot(path: Path, *, label: str) -> tuple[bytes | None, str | None, int | None, int | None, int | None]:
    return surface._preimage_snapshot(path, label=label)


def _entry(
    *, role: str, target: Path, before: bytes | None, before_sha256: str | None,
    before_mode: int | None, before_device: int | None, before_inode: int | None,
    after: bytes, after_mode: int, index: int,
) -> dict[str, object]:
    return {
        "role": role, "target": str(target), "before_bytes_b64": _b64(before),
        "before_sha256": before_sha256, "before_mode": before_mode,
        "before_device": before_device, "before_inode": before_inode,
        "after_bytes_b64": _b64(after), "after_sha256": _digest(after),
        "after_mode": after_mode, "phase": "PREPARED",
        "staged_name": f".{target.name}.qixi-basename-recovery-{index:03d}.tmp",
        "staged_device": None, "staged_inode": None,
        "installed_device": None, "installed_inode": None,
        "backup_name": f"{index:03d}-{role}.before",
    }


def _assert_document_invariants(
    before: Mapping[str, object], after: Mapping[str, object], *, mapping: Mapping[str, str]
) -> None:
    _assert_only_mapped_strings(before, after, mapping)
    if before.get("artifact_hashes") != after.get("artifact_hashes"):
        raise _error("basename recovery changes artifact hashes")
    if before.get("candidate_id") != after.get("candidate_id"):
        raise _error("basename recovery candidate drifts")


def _build_journal(authority: Mapping[str, object]) -> dict[str, object]:
    _source_root, source, source_receipt = _source_journal(
        authority, require_live_after_image=True
    )
    entries = source["entries"]
    assert isinstance(entries, list)
    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    package_root = Path(str(artifacts["record"]["path"])).parent
    legacy_namespace = legacy_public_artifact_root(package_root, authority)
    successor_namespace = public_artifact_root(package_root, authority)
    mapping: dict[str, str] = {}
    sidecars: list[tuple[Path, bytes, int]] = []
    documents: dict[str, tuple[Path, bytes, int]] = {}
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise _error("basename recovery source journal entry drifts")
        role, target = raw.get("role"), Path(str(raw.get("target")))
        after = _unb64(raw.get("after_bytes_b64"), label="source journal after")
        if after is None or raw.get("after_sha256") != _digest(after):
            raise _error("basename recovery source journal after drifts")
        mode = raw.get("after_mode")
        if isinstance(mode, bool) or not isinstance(mode, int):
            raise _error("basename recovery source journal mode drifts")
        if role == "cover_artifact":
            relative = _decode_legacy_relative(target, legacy_namespace=legacy_namespace)
            successor = public_artifact_path(successor_namespace, relative)
            if successor in {path for path, _, _ in sidecars}:
                raise _error("basename recovery successor artifact collides")
            mapping[str(target)] = str(successor)
            sidecars.append((successor, after, mode))
        elif role in {"record", "delivery_record", "publish", "state"}:
            documents[str(role)] = (target, after, mode)
        else:
            raise _error("basename recovery source journal role drifts")
    if set(documents) != {"record", "delivery_record", "publish", "state"} or not sidecars:
        raise _error("basename recovery source journal inventory drifts")
    record_before = json.loads(documents["record"][1])
    delivery_before = json.loads(documents["delivery_record"][1])
    publish_before = json.loads(documents["publish"][1])
    state_before = json.loads(documents["state"][1])
    if not all(isinstance(value, dict) for value in (record_before, delivery_before, publish_before, state_before)):
        raise _error("basename recovery source JSON drifts")
    if record_before != delivery_before:
        raise _error("basename recovery record mirror drifts")
    record_after = _replace_exact(record_before, mapping)
    publish_after = _replace_exact(publish_before, mapping)
    state_after = _replace_exact(state_before, mapping)
    assert isinstance(record_after, dict) and isinstance(publish_after, dict) and isinstance(state_after, dict)
    _assert_document_invariants(record_before, record_after, mapping=mapping)
    _assert_document_invariants(publish_before, publish_after, mapping=mapping)
    _assert_only_mapped_strings(state_before, state_after, mapping)
    if not any(value == str(successor_namespace) or value.startswith(str(successor_namespace)) for value in mapping.values()):
        raise _error("basename recovery successor mapping is empty")
    picks = state_after.get("picks")
    if not isinstance(picks, list) or not any(
        isinstance(row, Mapping)
        and row.get("candidate_id") == surface.CANDIDATE_ID
        and row.get("cover_path") in set(mapping.values())
        for row in picks
    ):
        raise _error("basename recovery state cover projection drifts")
    replacements = {
        "record": surface._json_bytes(record_after),
        "delivery_record": surface._json_bytes(record_after),
        "publish": surface._json_bytes(publish_after),
        "state": surface._json_bytes(state_after),
    }
    result_entries: list[dict[str, object]] = []
    for index, role in enumerate(("record", "delivery_record", "publish", "state")):
        target, sealed, mode = documents[role]
        before, digest, current_mode, device, inode = _entry_snapshot(target, label="basename recovery preimage")
        if before != sealed or digest != _digest(sealed) or current_mode != mode:
            raise _error("basename recovery committed document preimage drifts")
        result_entries.append(_entry(role=role, target=target, before=before, before_sha256=digest, before_mode=current_mode, before_device=device, before_inode=inode, after=replacements[role], after_mode=mode, index=index))
    for offset, (target, payload, mode) in enumerate(sorted(sidecars, key=lambda row: str(row[0])), start=len(result_entries)):
        before, digest, current_mode, device, inode = _entry_snapshot(target, label="basename recovery successor preimage")
        if before is not None and (before != payload or digest != _digest(payload) or current_mode != mode):
            raise _error("basename recovery successor target conflicts")
        result_entries.append(_entry(role="cover_artifact", target=target, before=before, before_sha256=digest, before_mode=current_mode, before_device=device, before_inode=inode, after=payload, after_mode=mode, index=offset))
    journal: dict[str, object] = {
        "schema_version": SCHEMA, "status": "PREPARED", "candidate_id": surface.CANDIDATE_ID,
        "recording_date": surface.RECORDING_DATE, "upload_enabled": False,
        "authority_sha256": authority["authority_sha256"],
        "source_journal_sha256": source["journal_sha256"],
        "source_final_receipt_sha256": _digest(source_receipt),
        "entries": result_entries, "path_mapping": mapping, "journal_sha256": "",
    }
    journal["journal_sha256"] = _journal_digest(journal)
    return journal


def _validate_journal(
    value: object,
    *,
    authority: Mapping[str, object],
    sealed_chat_authority_bytes: bytes | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _JOURNAL_KEYS:
        raise _error("basename recovery journal schema drifts")
    if (
        value.get("schema_version") != SCHEMA or value.get("candidate_id") != surface.CANDIDATE_ID
        or value.get("recording_date") != surface.RECORDING_DATE or value.get("upload_enabled") is not False
        or value.get("authority_sha256") != authority["authority_sha256"]
        or value.get("status") not in {"PREPARED", "COMMITTED"}
        or value.get("journal_sha256") != _journal_digest(value)
    ):
        raise _error("basename recovery journal binding drifts")
    source_root, source, source_receipt = _source_journal(
        authority,
        require_live_after_image=False,
        sealed_chat_authority_bytes=sealed_chat_authority_bytes,
    )
    del source_root
    if value["source_journal_sha256"] != source["journal_sha256"] or value["source_final_receipt_sha256"] != _digest(source_receipt):
        raise _error("basename recovery committed source chain drifts")
    mapping = value.get("path_mapping")
    entries = value.get("entries")
    if not isinstance(mapping, Mapping) or not mapping or not isinstance(entries, list) or len(entries) < 5:
        raise _error("basename recovery journal inventory drifts")
    targets: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != _ENTRY_KEYS:
            raise _error("basename recovery journal entry schema drifts")
        target = Path(str(entry["target"]))
        if target in targets or entry["role"] not in {"record", "delivery_record", "publish", "state", "cover_artifact"}:
            raise _error("basename recovery journal target drifts")
        targets.add(target)
        before, after = _unb64(entry["before_bytes_b64"], label="before"), _unb64(entry["after_bytes_b64"], label="after")
        if after is None or entry["after_sha256"] != _digest(after) or (before is None) != (entry["before_sha256"] is None):
            raise _error("basename recovery journal bytes drift")
    _validate_successor_shape(value, authority=authority, source=source)
    return value


def _validate_successor_shape(
    journal: Mapping[str, object], *, authority: Mapping[str, object], source: Mapping[str, object]
) -> None:
    """Prove the recovery is precisely source-after bytes plus locator mapping.

    The original public journal is no longer live after a valid recovery, so
    this derives the allowed successor bytes from its frozen after-image rather
    than treating the recovery journal as self-authorizing.
    """

    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    package_root = Path(str(artifacts["record"]["path"])).parent
    legacy_namespace = legacy_public_artifact_root(package_root, authority)
    successor_namespace = public_artifact_root(package_root, authority)
    mapping: dict[str, str] = {}
    source_sidecars: dict[Path, tuple[bytes, int]] = {}
    source_documents: dict[str, tuple[Path, bytes, int]] = {}
    raw_source_entries = source.get("entries")
    if not isinstance(raw_source_entries, list):
        raise _error("basename recovery source journal inventory drifts")
    for raw in raw_source_entries:
        assert isinstance(raw, Mapping)
        role, target = str(raw["role"]), Path(str(raw["target"]))
        after = _unb64(raw["after_bytes_b64"], label="source journal after")
        mode = raw["after_mode"]
        if after is None or isinstance(mode, bool) or not isinstance(mode, int):
            raise _error("basename recovery source journal bytes drift")
        if role == "cover_artifact":
            relative = _decode_legacy_relative(target, legacy_namespace=legacy_namespace)
            successor = public_artifact_path(successor_namespace, relative)
            mapping[str(target)] = str(successor)
            source_sidecars[successor] = (after, mode)
        elif role in {"record", "delivery_record", "publish", "state"}:
            source_documents[role] = (target, after, mode)
    if set(source_documents) != {"record", "delivery_record", "publish", "state"}:
        raise _error("basename recovery source journal inventory drifts")
    if dict(journal["path_mapping"]) != mapping:
        raise _error("basename recovery mapping drifts")
    record = json.loads(source_documents["record"][1])
    delivery = json.loads(source_documents["delivery_record"][1])
    publish = json.loads(source_documents["publish"][1])
    state = json.loads(source_documents["state"][1])
    if not all(isinstance(value, dict) for value in (record, delivery, publish, state)) or record != delivery:
        raise _error("basename recovery source JSON drifts")
    expected_documents = {
        "record": surface._json_bytes(_replace_exact(record, mapping)),
        "delivery_record": surface._json_bytes(_replace_exact(record, mapping)),
        "publish": surface._json_bytes(_replace_exact(publish, mapping)),
        "state": surface._json_bytes(_replace_exact(state, mapping)),
    }
    expected: dict[Path, tuple[bytes, bytes | None, int]] = {
        source_documents[role][0]: (expected_documents[role], source_documents[role][1], source_documents[role][2])
        for role in expected_documents
    }
    expected.update({path: (payload, None, mode) for path, (payload, mode) in source_sidecars.items()})
    actual: dict[Path, Mapping[str, object]] = {
        Path(str(entry["target"])): entry for entry in journal["entries"] if isinstance(entry, Mapping)
    }
    if set(actual) != set(expected):
        raise _error("basename recovery target inventory drifts")
    for target, (after, before, mode) in expected.items():
        entry = actual[target]
        if (
            _unb64(entry["after_bytes_b64"], label="after") != after
            or _unb64(entry["before_bytes_b64"], label="before") != before
            or entry["after_mode"] != mode
        ):
            raise _error("basename recovery successor projection drifts")


def validate_committed_successor(
    authority: Mapping[str, object],
    *,
    sealed_chat_authority_bytes: bytes | None = None,
    allow_terminal_successor: bool = False,
) -> None:
    """Public, read-only replay of the only allowed basename-recovery successor."""

    normalized = surface.validate_authority(authority)
    root = _recovery_root(normalized)
    journal = _load_existing(
        root,
        authority=normalized,
        sealed_chat_authority_bytes=sealed_chat_authority_bytes,
    )
    if journal is None or journal.get("status") != "COMMITTED":
        raise _error("basename recovery committed journal is missing")
    receipt_path = root / "final-receipt.json"
    surface._require_regular(receipt_path, label="basename recovery receipt")
    if receipt_path.read_bytes() != surface._json_bytes(_receipt(journal)):
        raise _error("basename recovery receipt drifts")
    if allow_terminal_successor:
        # The terminal transaction owns the current JSON bytes and CAS replay.
        # This predecessor still replays every formal gate through its sealed
        # after-image above, including source-fact receipt validation, but must
        # not reinterpret a verified terminal chat successor as predecessor
        # drift.
        return
    _postcommit_replay(journal, authority=normalized)


def _write_journal(root: Path, journal: dict[str, object], *, create: bool) -> None:
    journal["journal_sha256"] = _journal_digest(journal)
    path = root / "journal.json"
    payload = surface._json_bytes(journal)
    if create:
        surface._write_new(path, payload)
    else:
        surface._atomic_replace(path, payload)


def _load_existing(
    root: Path,
    *,
    authority: Mapping[str, object],
    sealed_chat_authority_bytes: bytes | None = None,
) -> dict[str, object] | None:
    if not os.path.lexists(root):
        return None
    _safe_root(root, create=False)
    journal_path = root / "journal.json"
    if not os.path.lexists(journal_path):
        raise _error("basename recovery journal is missing")
    return _validate_journal(
        _read_json(journal_path, label="journal"),
        authority=authority,
        sealed_chat_authority_bytes=sealed_chat_authority_bytes,
    )


def _verify_preimage(entry: Mapping[str, object], *, label: str) -> None:
    target = Path(str(entry["target"]))
    before, digest, mode, device, inode = _entry_snapshot(target, label=label)
    expected = _unb64(entry["before_bytes_b64"], label="before")
    if (before, digest, mode, device, inode) != (
        expected, entry["before_sha256"], entry["before_mode"], entry["before_device"], entry["before_inode"]
    ):
        raise _error("basename recovery preimage CAS drifts")


def _backup_path(root: Path, entry: Mapping[str, object]) -> Path:
    return root / "backups" / str(entry["backup_name"])


def _ensure_backup(root: Path, entry: Mapping[str, object]) -> None:
    before = _unb64(entry["before_bytes_b64"], label="before")
    if before is None:
        return
    backups = root / "backups"
    if not os.path.lexists(backups):
        backups.mkdir(mode=0o700)
    target = _backup_path(root, entry)
    if os.path.lexists(target):
        surface._require_regular(target, label="basename recovery backup")
        if target.read_bytes() != before:
            raise _error("basename recovery backup drifts")
        return
    surface._write_new(target, before)


def _checkpoint(root: Path, journal: dict[str, object], index: int, **changes: object) -> None:
    entries = journal["entries"]
    assert isinstance(entries, list) and isinstance(entries[index], Mapping)
    updated = dict(entries[index])
    updated.update(changes)
    entries[index] = updated
    _write_journal(root, journal, create=False)


def _verify_installed(entry: Mapping[str, object]) -> None:
    target = Path(str(entry["target"]))
    info = os.lstat(target)
    after = _unb64(entry["after_bytes_b64"], label="after")
    if after is None or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or (
        info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)
    ) != (entry["installed_device"], entry["installed_inode"], entry["after_mode"]) or target.read_bytes() != after:
        raise _error("basename recovery installed target drifts")


def _staged_snapshot(entry: Mapping[str, object]) -> tuple[Path, int, int]:
    target = Path(str(entry["target"]))
    staged = target.with_name(str(entry["staged_name"]))
    device, inode = entry["staged_device"], entry["staged_inode"]
    if isinstance(device, bool) or not isinstance(device, int) or isinstance(inode, bool) or not isinstance(inode, int):
        raise _error("basename recovery staged inode drifts")
    return staged, device, inode


def _verify_staged(entry: Mapping[str, object]) -> tuple[Path, int, int]:
    staged, device, inode = _staged_snapshot(entry)
    info = os.lstat(staged)
    after = _unb64(entry["after_bytes_b64"], label="after")
    if (
        after is None
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
        != (device, inode, entry["after_mode"])
        or staged.read_bytes() != after
    ):
        raise _error("basename recovery staged target drifts")
    return staged, device, inode


def _rollback(root: Path, journal: dict[str, object]) -> None:
    entries = journal["entries"]
    assert isinstance(entries, list)
    for index in reversed(range(len(entries))):
        entry = entries[index]
        assert isinstance(entry, Mapping)
        if entry["phase"] != "INSTALLED":
            continue
        _verify_installed(entry)
        target = Path(str(entry["target"]))
        before = _unb64(entry["before_bytes_b64"], label="before")
        if before is None:
            target.unlink()
        else:
            surface._atomic_replace(target, before)
        _checkpoint(root, journal, index, phase="PREPARED", staged_device=None, staged_inode=None, installed_device=None, installed_inode=None)


def _postcommit_replay(journal: Mapping[str, object], *, authority: Mapping[str, object]) -> None:
    """Re-read every successor inode; a COMMITTED receipt never masks drift."""

    _validate_journal(journal, authority=authority)
    if journal["status"] != "COMMITTED":
        raise _error("basename recovery journal is not committed")
    entries = journal["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, Mapping)
        if entry["phase"] != "INSTALLED":
            raise _error("basename recovery committed entry phase drifts")
        _verify_installed(entry)


def _commit(root: Path, journal: dict[str, object], *, authority: Mapping[str, object]) -> None:
    _validate_journal(journal, authority=authority)
    entries = journal["entries"]
    assert isinstance(entries, list)
    try:
        for index, raw in enumerate(entries):
            assert isinstance(raw, Mapping)
            entry = raw
            target = Path(str(entry["target"]))
            surface._require_safe_target_parent(target)
            if entry["phase"] == "INSTALLED":
                _verify_installed(entry)
                continue
            after = _unb64(entry["after_bytes_b64"], label="after")
            assert after is not None
            if entry["phase"] == "PREPARED":
                _verify_preimage(entry, label="basename recovery transaction")
                _ensure_backup(root, entry)
                staged = target.with_name(str(entry["staged_name"]))
                device, inode = create_staged_file(
                    staged,
                    payload=after,
                    sha256=str(entry["after_sha256"]),
                    mode=int(entry["after_mode"]),
                )
                _checkpoint(
                    root,
                    journal,
                    index,
                    phase="INSTALLING",
                    staged_device=device,
                    staged_inode=inode,
                )
                entry = journal["entries"][index]
                assert isinstance(entry, Mapping)
            if entry["phase"] != "INSTALLING":
                raise _error("basename recovery transaction phase drifts")
            staged, device, inode = _staged_snapshot(entry)
            target_info = os.lstat(target) if os.path.lexists(target) else None
            if target_info is not None and (target_info.st_dev, target_info.st_ino) == (device, inode):
                _verify_installed({**entry, "installed_device": device, "installed_inode": inode})
            else:
                _verify_staged(entry)
                _verify_preimage(entry, label="basename recovery install")
                clear_staged_file_owner(
                    staged,
                    device=device,
                    inode=inode,
                    sha256=str(entry["after_sha256"]),
                    mode=int(entry["after_mode"]),
                )
                os.replace(staged, target)
            info = os.lstat(target)
            if (info.st_dev, info.st_ino) != (device, inode):
                raise _error("basename recovery installed inode drifts")
            _checkpoint(root, journal, index, phase="INSTALLED", installed_device=device, installed_inode=inode)
            _verify_installed(journal["entries"][index])
    except Exception:
        _rollback(root, journal)
        raise
    journal["status"] = "COMMITTED"
    _write_journal(root, journal, create=False)
    _postcommit_replay(journal, authority=authority)


def _receipt(journal: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA, "status": "COMMITTED",
        "candidate_id": surface.CANDIDATE_ID, "recording_date": surface.RECORDING_DATE,
        "authority_sha256": journal["authority_sha256"], "journal_sha256": journal["journal_sha256"],
        "source_journal_sha256": journal["source_journal_sha256"],
        "entry_sha256": canonical_sha256(journal["entries"]),
    }
    result["receipt_sha256"] = canonical_sha256(result)
    return result


def _write_receipt(root: Path, journal: Mapping[str, object]) -> None:
    path = root / "final-receipt.json"
    expected = surface._json_bytes(_receipt(journal))
    if os.path.lexists(path):
        surface._require_regular(path, label="basename recovery receipt")
        if path.read_bytes() != expected:
            raise _error("basename recovery receipt drifts")
        return
    surface._write_new(path, expected)


def recover(*, apply: bool, repo_root: Path = surface.ROOT, authority: Mapping[str, object] | None = None) -> dict[str, object]:
    """Plan or apply the fixed legacy-dotfile successor migration."""

    normalized = surface.validate_authority(authority or surface.load_deployed_authority(repo_root))
    with surface._exclusive_runner_lock(Path(str(normalized["runtime_root"]))):
        root = _recovery_root(normalized)
        existing = _load_existing(root, authority=normalized)
        if existing is not None:
            if existing["status"] == "COMMITTED":
                _postcommit_replay(existing, authority=normalized)
                _write_receipt(root, existing)
                return {"schema_version": "qixi-post-correction-public-artifact-recovery-result.v1", "status": "RECOVERY_ALREADY_COMMITTED", "journal": str(root / "journal.json"), "upload_enabled": False}
        planned = _build_journal(normalized)
        if existing is not None:
            if existing["journal_sha256"] != planned["journal_sha256"]:
                raise _error("basename recovery existing journal differs from exact plan")
            journal = existing
        else:
            if not apply:
                return {"schema_version": "qixi-post-correction-public-artifact-recovery-plan.v1", "status": "PLAN_PASS", "journal": str(root / "journal.json"), "upload_enabled": False}
            _safe_root(root, create=True)
            _write_journal(root, planned, create=True)
            journal = planned
        if not apply:
            return {"schema_version": "qixi-post-correction-public-artifact-recovery-plan.v1", "status": "PLAN_PASS", "journal": str(root / "journal.json"), "upload_enabled": False}
        _commit(root, journal, authority=normalized)
        _postcommit_replay(journal, authority=normalized)
        _write_receipt(root, journal)
        return {"schema_version": "qixi-post-correction-public-artifact-recovery-result.v1", "status": "RECOVERY_COMMITTED", "journal": str(root / "journal.json"), "upload_enabled": False}
