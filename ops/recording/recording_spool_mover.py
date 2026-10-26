#!/usr/bin/env python3
"""One OCI3 mover daemon: sealed local recordings -> CD2 -> verified eviction.

No container/service control or provider-direct calls exist in this module.
The existing disk-guard service also calls this entry with --disk-guard-only,
so a stuck FUSE write cannot suppress local capacity warnings.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

try:
    from ops.recording import recording_archive as identity
    from ops.recording.cd2_status import CD2Status, GrpcWeb
except ModuleNotFoundError:  # installed host-side siblings, no repo/PYTHONPATH dependency
    import recording_archive as identity
    from cd2_status import CD2Status, GrpcWeb


def capacity(path: Path) -> dict[str, int]:
    info = os.statvfs(path)
    return {"available_bytes": info.f_bavail * info.f_frsize,
            "total_bytes": info.f_blocks * info.f_frsize}


def disk_guard(path: Path, minimum: int = 16 * 1024**3) -> dict[str, Any]:
    measured = capacity(path)
    result = {"event": "RECORDING_SPOOL_LOW_SPACE" if measured["available_bytes"] < minimum
              else "RECORDING_SPOOL_CAPACITY_OK", "path": str(path),
              "minimum_available_bytes": minimum, **measured, "checked_at_epoch": time.time()}
    print(json.dumps(result, sort_keys=True), flush=True)
    # When called by oci3-disk-guard's drop-in this is delivered to that
    # service's existing journal. The daemon also tags the same syslog channel.
    if measured["available_bytes"] < minimum:
        subprocess.run(["logger", "-p", "daemon.warning", "-t", "oci3-disk-guard",
                        json.dumps(result, sort_keys=True)], check=False, timeout=5)
    return result


def require_cloud_mount(path: Path, mount: Path = identity.CLOUD_MOUNT) -> None:
    """Only the mover probes CD2; never create directories below an absent mount."""
    result = subprocess.run(["findmnt", "-T", str(path), "-n", "-o", "TARGET,FSTYPE,SOURCE"],
                            capture_output=True, text=True, timeout=10, check=True)
    fields = result.stdout.strip().split()
    if len(fields) != 3 or fields[0] != str(mount) or not fields[1].startswith("fuse") or fields[2] != "CloudFS":
        raise identity.ArchiveError("archive destination is not the exact CD2 CloudFS mount")


def require_local_spool(path: Path) -> None:
    result = subprocess.run(["findmnt", "-T", str(path), "-n", "-o", "FSTYPE"],
                            capture_output=True, text=True, timeout=5, check=True)
    if result.stdout.strip() not in {"ext4", "xfs", "btrfs"}:
        raise identity.ArchiveError("recording spool must be on a local disk filesystem")


def open_files(paths: list[Path]) -> set[Path]:
    wanted = {str(path): path for path in paths}
    found: set[Path] = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                    if target in wanted:
                        found.add(wanted[target])
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return found


def append_event(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        out.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


class UploadObservations:
    def __init__(self, client: CD2Status, path: Path):
        self.client, self.path = client, path
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.rows = identity.read_json(path) if path.exists() else {}

    def note(self, row: dict[str, Any]) -> None:
        prefix = "/" + (identity.ARCHIVE_ROOT / identity.ROOM).relative_to(identity.CLOUD_MOUNT).as_posix() + "/"
        if not str(row.get("destPath", "")).startswith(prefix) or not row.get("key"):
            return
        with self.lock:
            # A later non-finished update invalidates an earlier Finished.
            self.rows[row["destPath"]] = {**row, "observed_at_epoch": time.time()}
            identity.atomic_json(self.path, self.rows)

    def get(self, path: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.rows.get(path, {}))

    def watch(self) -> None:
        delay = 5
        while True:
            try:
                for row in self.client.push_uploads(on_open=self.ready.set):
                    self.note(row)
                    delay = 5
            except Exception as exc:
                # Do not print remote messages (which may contain URLs/tokens).
                print(f"CD2_PUSH_RETRY error={type(exc).__name__} delay={delay}", flush=True)
            self.ready.clear()
            time.sleep(delay)
            delay = min(300, delay * 2)


class Mover:
    def __init__(self, *, spool: Path, archive_root: Path, state_path: Path,
                 archive_path: Path, client: CD2Status, observations: UploadObservations,
                 mount: Path = identity.CLOUD_MOUNT):
        self.spool, self.archive_root, self.mount = spool, archive_root, mount
        self.state_path, self.archive_path = state_path, archive_path
        self.client, self.observations = client, observations
        self.archive = identity.read_json(archive_path) if archive_path.exists() else {
            "schema_version": identity.ARCHIVE_SCHEMA, "segments": {}}
        if self.archive.get("schema_version") != identity.ARCHIVE_SCHEMA:
            raise identity.ArchiveError("archive state schema mismatch")
        self.events = archive_path.with_name("archive-tombstones.jsonl")

    def save(self) -> None:
        self.archive["generated_at_epoch"] = time.time()
        identity.atomic_json(self.archive_path, self.archive)

    def prepare(self, state: dict[str, Any], relative: str) -> dict[str, Any]:
        ledger = identity.sealed_ledger(state, relative)
        paths = identity.artifact_paths(relative)
        local = [self.spool / path for path in paths]
        if open_files(local):
            raise identity.ArchiveError("sealed artifact is still open by a process")
        row = {"phase": "pending", "files": {}, "created_at_epoch": time.time()}
        for name, path in zip(paths, local):
            before = identity.fingerprint(path)
            sha = identity.sha256_file(path)
            if identity.fingerprint(path) != before:
                raise identity.ArchiveError("artifact changed while hashing")
            row["files"][name] = {"size": before["size"], "sha256": sha,
                                  "local_fingerprint": before,
                                  "cloud_path": identity.cloud_path(name, archive_root=self.archive_root,
                                                                      mount=self.mount)}
        if row["files"][relative]["size"] != ledger["source_size"]:
            raise identity.ArchiveError("FLV differs from adapter finalized size")
        if identity.fingerprint(self.spool / relative)["mtime_ns"] != ledger["source_mtime_ns"]:
            raise identity.ArchiveError("FLV changed since adapter finalization")
        if row["files"][relative[:-4] + ".mp4"]["sha256"] != ledger["target_sha256"]:
            raise identity.ArchiveError("MP4 differs from adapter finalized SHA")
        self.archive["segments"][relative] = row
        self.save()
        return row

    def copy_file(self, name: str, item: dict[str, Any]) -> None:
        source, target = self.spool / name, self.archive_root / identity.ROOM / name
        if identity.fingerprint(source) != item["local_fingerprint"]:
            raise identity.ArchiveError("source preimage changed")
        # No mkdir/write before the exact CloudFS mount has been checked.
        require_cloud_mount(self.mount, self.mount)
        target.parent.mkdir(parents=True, exist_ok=True)
        require_cloud_mount(target.parent, self.mount)
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise identity.ArchiveError("destination traverses a symlink")
        try:
            out = target.open("xb")
        except FileExistsError:
            if not item.get("destination_created"):
                # An unrelated or crash-before-receipt object is never adopted.
                raise identity.ArchiveError("destination exists without this mover's creation receipt")
            out = target.open("r+b")
        else:
            item["destination_created"] = True
            item["copy_started_at_epoch"] = time.time()
            self.save()
        with out, source.open("rb") as inp:
            # Resume only our own create-only file, after comparing its entire
            # prefix. Never truncate/replace an existing object, even on retry.
            length = target.stat().st_size
            if length > item["size"]:
                raise identity.ArchiveError("destination larger than sealed source")
            remaining = length
            while remaining:
                count = min(1024 * 1024, remaining)
                if out.read(count) != inp.read(count):
                    raise identity.ArchiveError("destination prefix conflicts with source")
                remaining -= count
            for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if (identity.fingerprint(source) != item["local_fingerprint"]
                or identity.sha256_file(target) != item["sha256"]):
            raise identity.ArchiveError("copy SHA/preimage mismatch")
        item["copy_complete"] = True
        self.save()

    def verify_file(self, item: dict[str, Any]) -> None:
        path = item["cloud_path"]
        for upload in self.client.upload_list(Path(path).name):
            if upload.get("destPath") == path:
                self.observations.note(upload)
        upload = self.observations.get(path)
        if (upload.get("statusEnum") != 5 or upload.get("size") != item["size"]
                or upload.get("observed_at_epoch", 0) < item["copy_started_at_epoch"]):
            raise identity.ArchiveError("CD2 has not reported this file Finished")
        remote = self.client.refresh(path)
        if item.get("cloud_object_id") and remote.get("id") != item["cloud_object_id"]:
            raise identity.ArchiveError("refreshed cloud object identity changed")
        proof = {"method": "CD2_FINISHED_AND_FORCE_REFRESH", "upload": upload,
                 "object": remote, "forceRefresh": True, "checked_at_epoch": time.time()}
        if not identity.proof_valid(proof, path=path, size=item["size"]):
            raise identity.ArchiveError("CD2 Finished/refreshed identity/size gate failed")
        # Bind the verified copy's own placement after refresh, never reuse the
        # spool inode/mtime for its legitimate create-only archive copy.
        target = self.mount / path.lstrip("/")
        archive_fingerprint = identity.validate_artifact(
            target, item,
            fingerprint_key="archive_fingerprint" if "archive_fingerprint" in item else None,
        )
        item["archive_fingerprint"] = archive_fingerprint
        item["cloud_object_id"] = remote["id"]
        item["proof"] = proof
        self.save()

    def evict(self, relative: str, row: dict[str, Any]) -> None:
        current = identity.read_json(self.state_path)
        if not identity.verified_segment(current, self.archive, relative):
            raise identity.ArchiveError("archive/adapter identity changed before eviction")
        if relative in identity.disposition_references(current):
            row["retained_reason"] = "ADAPTER_DISPOSITION_PHYSICAL_EVIDENCE"
            self.save()
            return
        paths = [self.spool / name for name in row["files"]]
        if open_files(paths):
            raise identity.ArchiveError("local artifact is in use; eviction deferred")
        for name, item in row["files"].items():
            path = self.spool / name
            if item.get("deleted"):
                if path.exists() or path.is_symlink():
                    raise identity.ArchiveError("deleted recording path reappeared")
                continue
            if path.exists() or path.is_symlink():
                if (identity.fingerprint(path) != item["local_fingerprint"]
                        or identity.sha256_file(path) != item["sha256"]):
                    raise identity.ArchiveError("local file changed before eviction")
                if open_files([path]) or identity.fingerprint(path) != item["local_fingerprint"]:
                    raise identity.ArchiveError("local file became active/changed before unlink")
                # Persist the authorization BEFORE unlink, then record the
                # observed result and df. A crash can resume the exact subset.
                item["delete_authorized"] = True
                append_event(self.events, {"event": "DELETE_AUTHORIZED", "path": str(path),
                                           "size": item["size"], "sha256": item["sha256"],
                                           "proof": item["proof"]})
                self.save()
                path.unlink()
                identity.fsync_dir(path.parent)
            elif not item.get("delete_authorized"):
                raise identity.ArchiveError("local file missing without deletion authorization")
            item["deleted"] = True
            append_event(self.events, {"event": "LOCAL_DELETED", "path": str(path),
                                       "size": item["size"], "sha256": item["sha256"],
                                       "df": capacity(self.spool), "checked_at_epoch": time.time()})
            self.save()

    def tick(self, now: float | None = None) -> None:
        supplied_now = now
        now = time.time() if now is None else now
        state = identity.read_json(self.state_path)
        for relative in sorted(state.get("finalized", {})):
            row = self.archive["segments"].get(relative)
            if row and row.get("next_attempt_epoch", 0) > now:
                continue
            try:
                identity.sealed_ledger(state, relative)
                row = (row if row and row.get("phase") != "unprepared"
                       else self.prepare(state, relative))
                if row["phase"] != "verified":
                    for name in identity.artifact_paths(relative):
                        item = row["files"][name]
                        if not item.get("copy_complete"):
                            self.copy_file(name, item)
                        self.verify_file(item)
                    row["phase"] = "verified"
                    row.pop("error", None)
                    self.save()
                else:
                    # A retry after verification/crash obtains a new refreshed
                    # listing before deleting any remaining local preimage.
                    for item in row["files"].values():
                        if not item.get("deleted"):
                            self.verify_file(item)
                self.evict(relative, row)
                row.pop("error", None)
                row.pop("next_attempt_epoch", None)
                row["failures"] = 0
            except Exception as exc:
                row = row or self.archive["segments"].setdefault(relative, {"phase": "unprepared"})
                row["failures"] = min(16, row.get("failures", 0) + 1)
                retry_from = time.time() if supplied_now is None else now
                row["next_attempt_epoch"] = retry_from + min(300, 5 * 2**(row["failures"] - 1))
                row["error"] = type(exc).__name__
                # Local contract errors have safe, concrete messages. Network
                # exceptions may contain authentication/URL details: omit them.
                if isinstance(exc, identity.ArchiveError):
                    row["reason"] = str(exc)
            self.save()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk-guard-only", action="store_true")
    parser.add_argument("--spool", type=Path, default=identity.SPOOL_ROOT / identity.ROOM)
    parser.add_argument("--state-path", type=Path, default=identity.STATE_DIR / "adapter-state.json")
    parser.add_argument("--archive-state", type=Path, default=identity.ARCHIVE_STATE)
    parser.add_argument("--cd2-endpoint", default="http://127.0.0.1:19798")
    parser.add_argument("--cd2-token-file", type=Path, default=Path("/opt/bilive/recording/cd2-token"))
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    require_local_spool(args.spool)
    disk_guard(args.spool)
    if args.disk_guard_only:
        return 0
    args.archive_state.parent.mkdir(parents=True, exist_ok=True)
    with args.archive_state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = CD2Status(GrpcWeb(args.cd2_endpoint, args.cd2_token_file))
        observations = UploadObservations(client, args.archive_state.with_name("cd2-uploads.json"))
        threading.Thread(target=observations.watch, daemon=True).start()
        mover = Mover(spool=args.spool, archive_root=identity.ARCHIVE_ROOT,
                      state_path=args.state_path, archive_path=args.archive_state,
                      client=client, observations=observations)
        last_capacity_check = 0.0
        while True:
            if time.time() - last_capacity_check >= 60:
                disk_guard(args.spool)
                last_capacity_check = time.time()
            try:
                # Subscribe before any create: a rapid upload can otherwise
                # finish before the daemon has a chance to observe its event.
                if observations.ready.is_set():
                    mover.tick()
                else:
                    print("SPOOL_WAIT_CD2_STATUS_STREAM", flush=True)
            except Exception as exc:
                print(f"SPOOL_RETRY error={type(exc).__name__}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
