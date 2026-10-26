"""Shared recording identity and local-to-CD2 placement contract.

The adapter's CLOSED/finalized ledgers remain the seal authority.  Moving a
file changes its placement, never its room/date/stem or content identity.
This module deliberately performs no cloud I/O.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any

try:
    from ops.recording.recording_topology import load_topology
except ModuleNotFoundError:
    from recording_topology import load_topology

TOPOLOGY = load_topology()
ROOM = TOPOLOGY.room
SPOOL_ROOT = TOPOLOGY.spool_root
CLOUD_MOUNT = TOPOLOGY.cloud_mount
ARCHIVE_ROOT = TOPOLOGY.archive_root
STATE_DIR = TOPOLOGY.state_dir
ARCHIVE_STATE = STATE_DIR / "archive-state.json"
ARCHIVE_SCHEMA = "recording-local-first-archive.v1"
SUFFIXES = (".flv", ".xml", ".jsonl", ".meta.json", ".mp4")


class ArchiveError(ValueError):
    pass


def relative_flv(value: str, room: str = ROOM) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        rf"20\d{{2}}-\d{{2}}-\d{{2}}/{re.escape(str(room))}_20\d{{6}}-\d{{2}}-\d{{2}}-\d{{2}}\.flv", value
    ):
        raise ArchiveError("invalid recording relative path")
    return value


def artifact_paths(relative: str, room: str = ROOM) -> list[str]:
    stem = relative_flv(relative, room)[:-4]
    return [stem + suffix for suffix in SUFFIXES]


def cloud_path(relative: str, *, archive_root: Path = ARCHIVE_ROOT,
               mount: Path = CLOUD_MOUNT, room: str = ROOM) -> str:
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts or len(parsed.parts) != 2:
        raise ArchiveError("invalid artifact path")
    return "/" + (archive_root / str(room) / relative).relative_to(mount).as_posix()


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ArchiveError(f"not a regular JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ArchiveError("JSON must be an object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_consumer_state(path: Path, *, primary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read old direct-115 history beside the new capture ledger, without writes.

    Capture and eviction always use the unmerged new ledger. Only downstream
    historical media readers use this view; overlapping identities fail closed.
    """
    state = dict(read_json(path) if primary is None else primary)
    if path.parent.name != TOPOLOGY.state_dir.name:
        return state
    legacy = path.parent.parent / TOPOLOGY.legacy_state_dirs[-1].name / "adapter-state.json"
    if not legacy.exists():
        return state
    old = read_json(legacy)
    if old.get("schema_version") != state.get("schema_version"):
        raise ArchiveError("legacy/current adapter schemas disagree")
    for field in ("webhook_files", "finalized", "source_dispositions",
                  "source_disposition_identity_rebinds", "source_disposition_identity_rebind_tasks"):
        historical, current = old.get(field, {}), state.get(field, {})
        if not isinstance(historical, dict) or not isinstance(current, dict):
            raise ArchiveError("legacy/current adapter ledger is malformed")
        for key in historical.keys() & current.keys():
            if historical[key] != current[key]:
                raise ArchiveError("legacy/current recording identity overlaps: " + key)
        state[field] = {**historical, **current}
    return state


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fingerprint(path: Path) -> dict[str, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArchiveError(f"not a single-link regular file: {path}")
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "inode": info.st_ino, "device": info.st_dev}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_artifact(path: Path, item: dict[str, Any], *,
                      fingerprint_key: str | None = None) -> dict[str, int]:
    """Revalidate recorded content plus the fingerprint of this placement.

    The local and archive copies share a SHA, but have different legitimate
    inode/device/time bindings. Each is compared to its own saved placement;
    a cache keyed only by stat must not hide a same-size rewrite.
    """
    before = fingerprint(path)
    if (before["size"] != item["size"]
            or (fingerprint_key is not None and before != item.get(fingerprint_key))):
        raise ArchiveError(f"artifact fingerprint drifted: {path}")
    if (not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
            or sha256_file(path) != item["sha256"]):
        raise ArchiveError(f"artifact SHA-256 drifted: {path}")
    if fingerprint(path) != before:
        raise ArchiveError(f"artifact drifted while hashing: {path}")
    return before


def sealed_ledger(state: dict[str, Any], relative: str) -> dict[str, Any]:
    """Use exactly the adapter's native FileClosed + finalized binding.

    N100 imports have their own natural-seal provenance in the finalized row;
    no synthetic webhook is required for those rows.
    """
    relative_flv(relative)
    row = state.get("finalized", {}).get(relative)
    if not isinstance(row, dict) or not row.get("finalized_at"):
        raise ArchiveError("recording has not been finalized by adapter")
    evidence = state.get("webhook_files", {}).get(relative)
    imported = row.get("source_authority") == "N100_115_ARCHIVE_STATE_NATURAL_SEAL"
    if not imported and (not isinstance(evidence, dict) or evidence.get("status") != "CLOSED"
                         or evidence.get("file_size") != row.get("source_size")):
        raise ArchiveError("recording lacks matching adapter FileClosed evidence")
    if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("target_sha256", ""))):
        raise ArchiveError("finalized MP4 has no SHA-256")
    return row


def proof_valid(proof: Any, *, path: str, size: int) -> bool:
    if not isinstance(proof, dict):
        return False
    upload, remote = proof.get("upload", {}), proof.get("object", {})
    if not isinstance(upload, dict) or not isinstance(remote, dict):
        return False
    return (
        proof.get("method") == "CD2_FINISHED_AND_FORCE_REFRESH"
        and upload.get("destPath") == path and upload.get("size") == size
        and upload.get("statusEnum") == 5 and upload.get("errorMessage", "") == ""
        and bool(upload.get("key"))
        and proof.get("forceRefresh") is True
        and remote.get("fullPathName") == path and remote.get("size") == size
        and bool(remote.get("id")) and remote.get("isDirectory") is False
        and remote.get("isCloudFile") is True
        and isinstance(proof.get("checked_at_epoch"), (float, int))
    )


def verified_segment(state: dict[str, Any], archive: dict[str, Any], relative: str) -> bool:
    """A persistent, local receipt usable by adapter and downstream readers."""
    try:
        ledger = sealed_ledger(state, relative)
        row = archive["segments"][relative]
        if archive.get("schema_version") != ARCHIVE_SCHEMA or row.get("phase") != "verified":
            return False
        files = row["files"]
        if set(files) != set(artifact_paths(relative)):
            return False
        for name, item in files.items():
            expected_path = cloud_path(name)
            if (item.get("cloud_path") != expected_path
                    or not isinstance(item.get("archive_fingerprint"), dict)
                    or item["archive_fingerprint"].get("size") != item["size"]
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                    or not proof_valid(item.get("proof"), path=expected_path, size=item["size"])):
                return False
        return (files[relative]["size"] == ledger["source_size"]
                and files[relative[:-4] + ".mp4"]["sha256"] == ledger["target_sha256"])
    except (KeyError, TypeError, ArchiveError):
        return False


def archive_date_issues(state: dict[str, Any], archive: dict[str, Any], date: str) -> list[str]:
    """Only archived OCI3 native pieces can enter the cloud consumer lane."""
    issues = []
    for relative, evidence in state.get("webhook_files", {}).items():
        if relative.split("/", 1)[0] != date:
            continue
        disposition = state.get("source_dispositions", {}).get(relative, {})
        if (str(disposition.get("status", "")).startswith("IGNORED")
                or disposition.get("action") == "IGNORED_TRUNCATED_RECONNECT_FRAGMENT"):
            continue
        if not isinstance(evidence, dict) or not verified_segment(state, archive, relative):
            issues.append(relative)
    return sorted(issues)


def disposition_references(state: dict[str, Any]) -> set[str]:
    """Physical evidence for dispositions must keep its original placement.

    These rare recovery pieces can be archived, but their local bytes remain
    bound to the adapter's existing physical-evidence validation contract.
    """
    protected: set[str] = set()
    for relative, row in state.get("source_dispositions", {}).items():
        protected.add(relative)
        # Existing typed dispositions contain exact successor relative paths
        # and/or absolute evidence paths. Collect only known recording names.
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str):
                parts = PurePosixPath(value).parts
                if len(parts) >= 2:
                    candidate = "/".join(parts[-2:])
                    for suffix in SUFFIXES:
                        if candidate.endswith(suffix):
                            try:
                                protected.add(relative_flv(candidate[:-len(suffix)] + ".flv"))
                            except ArchiveError:
                                pass
        visit(row)
    return protected


def retained_validation_source(relative: str, archive: dict[str, Any],
                               *, spool: Path | None = None) -> Path | None:
    """Resolve physical disposition evidence without confusing it with bytes.

    The cloud piece keeps the same SHA. Its original local evidence remains
    available for the existing inode/metadata-based disposition validator.
    """
    row = archive.get("segments", {}).get(relative, {})
    if row.get("retained_reason") != "ADAPTER_DISPOSITION_PHYSICAL_EVIDENCE":
        return None
    root = spool or SPOOL_ROOT / ROOM
    for name in artifact_paths(relative):
        item, path = row["files"][name], root / name
        try:
            validate_artifact(path, item, fingerprint_key="local_fingerprint")
        except ArchiveError as exc:
            raise ArchiveError("retained disposition evidence changed") from exc
    return root / relative
