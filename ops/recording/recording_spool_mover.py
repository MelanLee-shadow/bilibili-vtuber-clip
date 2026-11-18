#!/usr/bin/env python3
"""One OCI3 mover daemon: sealed local recordings -> CD2 (115 + 123) -> verified eviction.

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
    from ops.recording.cd2_status import CD2Error, CD2IdleTimeout, CD2Status, GrpcWeb, error_code
except ModuleNotFoundError:  # installed host-side siblings, no repo/PYTHONPATH dependency
    import recording_archive as identity
    from cd2_status import CD2Error, CD2IdleTimeout, CD2Status, GrpcWeb, error_code


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
    def __init__(self, client: CD2Status, path: Path, roots: tuple[Path, ...] | None = None):
        self.client, self.path = client, path
        # Given roots are fixed now; None = the 115 archive only, read at call time.
        self.prefixes = None if roots is None else tuple(self.prefix(root) for root in roots)
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.rows = identity.read_json(path) if path.exists() else {}

    @staticmethod
    def prefix(root: Path) -> str:
        return "/" + (root / identity.ROOM).relative_to(identity.CLOUD_MOUNT).as_posix() + "/"

    def note(self, row: dict[str, Any]) -> None:
        prefixes = self.prefixes or (self.prefix(identity.ARCHIVE_ROOT),)
        if not str(row.get("destPath", "")).startswith(prefixes) or not row.get("key"):
            return
        with self.lock:
            now = time.time()
            previous = self.rows.get(row["destPath"], {})
            # progress_at_epoch: when this upload last changed (task, state or
            # bytes). Re-reading an unchanged stuck task does not move it.
            same = all(previous.get(key) == row.get(key)
                       for key in ("key", "statusEnum", "transferedBytes"))
            progress = previous.get("progress_at_epoch", now) if same and previous else now
            # A later non-finished update invalidates an earlier Finished.
            self.rows[row["destPath"]] = {**row, "observed_at_epoch": now,
                                          "progress_at_epoch": progress}
            identity.atomic_json(self.path, self.rows)

    def get(self, path: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.rows.get(path, {}))

    def watch(self) -> None:
        # This runs in the status thread: even the startup check cannot hold
        # up copying. It never creates credentials or changes CD2 state.
        try:
            self.client.check_auth()
        except Exception as exc:
            print(f"CD2_AUTH_CHECK_FAILED code={error_code(exc)}", flush=True)
        else:
            print("CD2_AUTH_CHECK_OK", flush=True)
        delay = 5
        while True:
            failed = False
            try:
                for row in self.client.push_uploads(on_open=self.ready.set):
                    self.note(row)
                    delay = 5
            except CD2IdleTimeout:
                # Only a between-frame timeout on an opened PushMessage is
                # classified idle. Connect/partial-frame failures still retry.
                print("CD2_PUSH_IDLE code=CD2_PUSH_IDLE_TIMEOUT", flush=True)
                delay = 5
                continue
            except Exception as exc:
                # Do not print remote messages (which may contain URLs/tokens).
                print(f"CD2_PUSH_RETRY error={type(exc).__name__} code={error_code(exc)} delay={delay}", flush=True)
                failed = True
            else:
                delay = 5
            self.ready.clear()
            time.sleep(delay)
            delay = min(300, delay * 2) if failed else 5


# A copied file CD2 no longer tracks (Finished missed, task gone) or whose task
# has not moved for this long is uploaded again: proof stays a fresh Finished.
REUPLOAD_GRACE_SECONDS = 30 * 60
UPLOAD_STALL_SECONDS = 2 * 3600
REUPLOAD_ATTEMPTS = 3


class Mover:
    def __init__(self, *, spool: Path, archive_root: Path, state_path: Path,
                 archive_path: Path, client: CD2Status, observations: UploadObservations,
                 mount: Path = identity.CLOUD_MOUNT, backup_root: Path | None = None,
                 lane_root: Path | None = None):
        self.spool, self.archive_root, self.mount = spool, archive_root, mount
        self.lane_root = lane_root


        self.backup_root = backup_root
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
        if self.backup_root is not None:
            for name, item in row["files"].items():
                self.backup_place(name, item)
        self.archive["segments"][relative] = row
        self.save()
        return row

    def backup_place(self, name: str, item: dict[str, Any]) -> dict[str, Any]:
        """The 123 copy's own placement record, kept beside the 115 fields.

        Rows prepared before the backup existed get the record on first use.
        """
        assert self.backup_root is not None
        expected = identity.cloud_path(name, archive_root=self.backup_root, mount=self.mount)
        place = item.setdefault("backup", {"cloud_path": expected})
        if place.get("cloud_path") != expected:
            raise identity.ArchiveError("123 backup placement differs from the topology")
        return place

    def backup_proven(self, name: str, item: dict[str, Any]) -> bool:
        place = item.get("backup")
        if self.backup_root is None or not isinstance(place, dict):
            return False
        expected = identity.cloud_path(name, archive_root=self.backup_root, mount=self.mount)
        return (place.get("cloud_path") == expected and bool(place.get("cloud_object_id"))
                and identity.proof_valid(place.get("proof"), path=expected, size=item["size"]))

    def copy_file(self, name: str, item: dict[str, Any], place: dict[str, Any] | None = None,
                  root: Path | None = None) -> None:
        """Copy to the 115 archive (place=item) or to the 123 backup placement.

        Content identity (size, SHA, local preimage) always comes from item;
        creation receipts are per placement.
        """
        place = item if place is None else place
        root = self.archive_root if root is None else root
        source, target = self.spool / name, root / identity.ROOM / name
        if identity.fingerprint(source) != item["local_fingerprint"]:
            raise identity.ArchiveError("source preimage changed")
        # No mkdir/write before the exact CloudFS mount has been checked.
        require_cloud_mount(self.mount, self.mount)
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise identity.ArchiveError("destination traverses a symlink")
        length = 0
        exists = target.exists()
        if exists:
            if not place.get("destination_created"):
                raise identity.ArchiveError("destination exists without this mover's creation receipt")
            length = target.stat().st_size
            if length > item["size"]:
                raise identity.ArchiveError("destination larger than sealed source")
        # Spool and CD2's local cache share the root disk. Auth may stay
        # unavailable indefinitely, so reserve space without assuming eviction.
        pending_bytes = item["size"] - length
        minimum = 8 * 1024**3 + pending_bytes
        if (pending_bytes or not exists) and capacity(self.spool)["available_bytes"] < minimum:
            disk_guard(self.spool, minimum=minimum)
            raise identity.ArchiveError("copy would leave less than 8 GiB for local recording")
        target.parent.mkdir(parents=True, exist_ok=True)
        require_cloud_mount(target.parent, self.mount)
        try:
            out = target.open("xb")
        except FileExistsError:
            if not place.get("destination_created"):
                # An unrelated or crash-before-receipt object is never adopted.
                raise identity.ArchiveError("destination exists without this mover's creation receipt")
            out = target.open("r+b")
        else:
            place["destination_created"] = True
            place["copy_started_at_epoch"] = time.time()
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
        place["copy_complete"] = True
        place["copy_completed_at_epoch"] = time.time()
        self.save()

    def placement_name(self, item: dict[str, Any], place: dict[str, Any]) -> tuple[str, Path]:
        """The artifact's spool name and archive root, checked against the topology."""
        path = place["cloud_path"]
        name = "/".join(Path(path).parts[-2:])
        for root in (self.archive_root, self.backup_root):
            if root is not None and identity.cloud_path(name, archive_root=root, mount=self.mount) == path:
                if identity.cloud_path(name, archive_root=self.archive_root, mount=self.mount) != item["cloud_path"]:
                    break
                return name, root
        raise identity.ArchiveError("cloud placement differs from the topology")

    def reupload(self, item: dict[str, Any], place: dict[str, Any], *, reason: str,
                 upload: dict[str, Any]) -> None:
        """Upload an unproven copy again, so CD2 reports a fresh Finished.

        CD2 lost the task (a Finished missed between push reconnects, then gone
        from its queue) or the task stopped moving. A force-refreshed listing
        shows a stuck upload at full size, so it cannot stand in for Finished.
        Only this mover's own unproven object is removed, and only while the
        local preimage still matches the sealed SHA; local deletion still
        needs the normal proof for every placement.
        """
        name, root = self.placement_name(item, place)
        if place.get("proof"):
            raise identity.ArchiveError("a proven cloud copy is never uploaded again")
        if not place.get("destination_created"):
            raise identity.ArchiveError("cloud object lacks this mover's creation receipt")
        source = self.spool / name
        if (identity.fingerprint(source) != item["local_fingerprint"]
                or identity.sha256_file(source) != item["sha256"]):
            raise identity.ArchiveError("local preimage changed; cloud copy not uploaded again")
        attempts = place.setdefault("reuploads", [])
        if not attempts or attempts[-1].get("reset_at_epoch"):
            if len(attempts) >= REUPLOAD_ATTEMPTS:
                raise identity.ArchiveError("CD2 re-upload attempts exhausted; local copy kept")
            attempts.append({"reason": reason, "requested_at_epoch": time.time(),
                             "previous_copy_started_at_epoch": place.get("copy_started_at_epoch"),
                             "upload": upload})
            append_event(self.events, {"event": "CLOUD_REUPLOAD_AUTHORIZED", "cloud_path": place["cloud_path"],
                                       "reason": reason, "attempt": len(attempts),
                                       "size": item["size"], "sha256": item["sha256"],
                                       "checked_at_epoch": time.time()})
            self.save()
        require_cloud_mount(self.mount, self.mount)
        target = root / identity.ROOM / name
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise identity.ArchiveError("destination traverses a symlink")
        if target.exists():
            if not target.is_file() or target.stat().st_size > item["size"]:
                raise identity.ArchiveError("cloud object is not this mover's copy")
            target.unlink()
        if target.exists():
            raise identity.ArchiveError("cloud object still present after unlink; re-upload deferred")
        for key in ("destination_created", "copy_started_at_epoch", "copy_complete",
                    "copy_completed_at_epoch", "archive_fingerprint", "cloud_object_id"):
            place.pop(key, None)
        attempts[-1]["reset_at_epoch"] = time.time()
        append_event(self.events, {"event": "CLOUD_COPY_RESET", "cloud_path": place["cloud_path"],
                                   "reason": reason, "checked_at_epoch": time.time()})
        self.save()

    def unfinished(self, item: dict[str, Any], place: dict[str, Any], upload: dict[str, Any],
                   queued: bool) -> None:
        """Decide a copy CD2 has not reported Finished: wait, or upload again. Always raises."""
        now = time.time()
        copied_at = place.get("copy_completed_at_epoch", place.get("copy_started_at_epoch", now))
        if not place.get("proof") and place.get("copy_complete"):
            if queued and now - upload.get("progress_at_epoch", now) >= UPLOAD_STALL_SECONDS:
                self.reupload(item, place, reason="CD2_UPLOAD_STALLED", upload=upload)
                raise identity.ArchiveError("CD2 upload stalled; uploading again")
            if not queued and now - copied_at >= REUPLOAD_GRACE_SECONDS:
                self.reupload(item, place, reason="CD2_NO_UPLOAD_RECORD", upload=upload)
                raise identity.ArchiveError("CD2 has no record of this upload; uploading again")
        raise identity.ArchiveError("CD2 has not reported this file Finished")

    def verify_file(self, item: dict[str, Any], place: dict[str, Any] | None = None) -> None:
        place = item if place is None else place
        path = place["cloud_path"]
        listed = [upload for upload in self.client.upload_list(Path(path).name)
                  if upload.get("destPath") == path]
        # A task left behind by an earlier copy must not mask a newer Finished.
        for upload in sorted(listed, key=lambda upload: upload.get("statusEnum") == 5):
            self.observations.note(upload)
        queued = bool(listed) and all(upload.get("statusEnum") != 5 for upload in listed)
        upload = self.observations.get(path)
        if (upload.get("statusEnum") != 5 or upload.get("size") != item["size"]
                or upload.get("observed_at_epoch", 0) < place["copy_started_at_epoch"]):
            self.unfinished(item, place, upload, queued)
        remote = self.client.refresh(path)
        if place.get("cloud_object_id") and remote.get("id") != place["cloud_object_id"]:
            raise identity.ArchiveError("refreshed cloud object identity changed")
        proof = {"method": "CD2_FINISHED_AND_FORCE_REFRESH", "upload": upload,
                 "object": remote, "forceRefresh": True, "checked_at_epoch": time.time()}
        if not identity.proof_valid(proof, path=path, size=item["size"]):
            raise identity.ArchiveError("CD2 Finished/refreshed identity/size gate failed")
        # Bind the verified copy's own placement after refresh, never reuse the
        # spool inode/mtime for its legitimate create-only archive copy.
        target = self.mount / path.lstrip("/")
        content = {"size": item["size"], "sha256": item["sha256"],
                   "archive_fingerprint": place.get("archive_fingerprint")}
        archive_fingerprint = identity.validate_artifact(
            target, content,
            fingerprint_key="archive_fingerprint" if "archive_fingerprint" in place else None,
        )
        place["archive_fingerprint"] = archive_fingerprint
        place["cloud_object_id"] = remote["id"]
        place["proof"] = proof
        self.save()

    def archive_upload_in_flight(self, state: dict[str, Any]) -> bool:
        """A finalized segment is on its way to 115: copied and not proven yet, or not copied yet.

        A segment that has not been copied and is failing (its row carries an error) does not hold
        123 back, so one broken segment cannot keep every other one on the local disk."""
        for relative in state.get("finalized", {}):
            row = self.archive["segments"].get(relative) or {}
            if row.get("phase") == "verified":
                continue
            if any(item.get("copy_complete") for item in (row.get("files") or {}).values()) or not row.get("error"):
                return True
        return False

    def verify_each(self, placements: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> None:
        """Verify every placement; one unfinished copy does not hold back the rest.

        A contract failure is raised after the others were tried. A CD2/RPC
        failure stops at once (each further call would only wait out the same outage).
        """
        failed: identity.ArchiveError | None = None
        for item, place in placements:
            try:
                self.verify_file(item, place)
            except identity.ArchiveError as exc:
                failed = failed or exc
        if failed is not None:
            raise failed

    def lane_wants(self, relative: str, row: dict[str, Any]) -> set[str]:
        """The FLV/XML of this segment the clip lane still lacks (oci3_lane_spool_copy.sh).

 公开规则：边录边传有个坏处是下播后才开始选片，原始录播文件已经不见了，还得重新下;
 「为什么一份文件要占这么多」— the host keeps one copy of a recording. While the
 stream is live the copy waits, and eviction hands these files to the lane by renaming them
 (same disk, no second copy) instead of deleting them. A day the lane does not copy (no day
 directory) or takes from another source (`.source` not "spool") wants nothing."""
        if self.lane_root is None:
            return set()
        day = self.lane_root / Path(relative).parent
        source = day / ".source"
        if not day.is_dir() or (source.exists() and source.read_text(encoding="utf-8").strip() != "spool"):
            return set()
        return {name for name, item in row["files"].items()
                if not item.get("deleted") and name.endswith((".flv", ".xml"))
                and not ((self.lane_root / name).is_file()
                         and (self.lane_root / name).stat().st_size == item["size"])}

    def hand_to_lane(self, path: Path, name: str) -> None:
        """Rename the sealed file into the lane's day directory, owned like that directory."""
        assert self.lane_root is not None
        target = self.lane_root / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise identity.ArchiveError("lane path is not a regular file")
        owner = target.parent.stat()
        os.replace(path, target)
        os.chown(target, owner.st_uid, owner.st_gid)
        os.chmod(target, 0o644)
        identity.fsync_dir(target.parent)

    def evict(self, relative: str, row: dict[str, Any]) -> None:
        current = identity.read_json(self.state_path)
        if not identity.verified_segment(current, self.archive, relative):
            raise identity.ArchiveError("archive/adapter identity changed before eviction")
        stubs = None
        if relative in identity.disposition_references(current):
            # A connection stub's successor is released once 115 and 123 are
            # both verified; any other bound evidence keeps its local bytes.
            stubs = (identity.stub_successor_rows(current, relative)
                     if self.backup_root is not None else None)
            if stubs is None:
                row["retained_reason"] = "ADAPTER_DISPOSITION_PHYSICAL_EVIDENCE"
                self.save()
                return
        if self.backup_root is not None and not all(
                item.get("deleted") or self.backup_proven(name, item)
                for name, item in row["files"].items()):
            raise identity.ArchiveError("123 backup not verified; eviction deferred")
        wanted = self.lane_wants(relative, row)
        paths = [self.spool / name for name in row["files"]]
        if open_files(paths):
            raise identity.ArchiveError("local artifact is in use; eviction deferred")
        if stubs is not None:
            release = row.get("disposition_release")
            expected = {stub: stub_row["canonical_integrity"]["canonical_json_sha256"]
                        for stub, stub_row in stubs.items()}
            if release is None:
                # Persisted before any unlink, so validators always see either
                # the exact local file or this receipt.
                release = identity.successor_release(row, relative, stubs, self.spool)
                row["disposition_release"] = release
                row.pop("retained_reason", None)
                append_event(self.events, {"event": "DISPOSITION_SUCCESSOR_RELEASED",
                                           "relative": relative, "dispositions": expected,
                                           "checked_at_epoch": time.time()})
                self.save()
            elif release.get("dispositions") != expected:
                raise identity.ArchiveError("disposition changed after successor release")
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
                if name in wanted:
                    self.hand_to_lane(path, name)
                    item["handed_to_lane"] = True
                else:
                    path.unlink()
                identity.fsync_dir(path.parent)
            elif not item.get("delete_authorized"):
                raise identity.ArchiveError("local file missing without deletion authorization")
            item["deleted"] = True
            append_event(self.events, {"event": "LOCAL_HANDED_TO_LANE" if item.get("handed_to_lane") else "LOCAL_DELETED",
                                       "path": str(path),
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
                names = identity.artifact_paths(relative)
                live = [name for name in names if not row["files"][name].get("deleted")]
                if (row["phase"] == "verified" and row.get("retained_reason")
                        and relative in identity.disposition_references(state)
                        and (self.backup_root is None
                             or identity.stub_successor_rows(state, relative) is None)
                        and (self.backup_root is None
                             or all(self.backup_proven(name, row["files"][name]) for name in live))):
                    # Kept local only as adapter evidence, so nothing here will
                    # be deleted: do not re-read the archived copies through
                    # CD2 on every tick (that re-downloaded ~2 GiB forever).
                    continue
                if row["phase"] != "verified":
                    for name in names:
                        item = row["files"][name]
                        if not item.get("copy_complete"):
                            self.copy_file(name, item)
                if row["phase"] != "verified":
                    # Verification needs authenticated CD2 evidence; copying
                    # each sealed artifact does not. One file CD2 has not
                    # reported does not block proving the rest of the group.
                    self.verify_each([(row["files"][name], None) for name in names])
                    row["phase"] = "verified"
                    row.pop("error", None)
                    self.save()
                    rechecked = True
                else:
                    rechecked = False




                if self.backup_root is not None and not self.archive_upload_in_flight(state):
                    for name in live:
                        item = row["files"][name]
                        place = self.backup_place(name, item)
                        if not place.get("copy_complete"):
                            self.copy_file(name, item, place, self.backup_root)
                # 123 is checked before any 115 re-check, so waiting for the
                # backup upload does not re-read the 115 copies on each retry.
                # A backup proven once stays proven; 115 stays the archive of
                # record and is re-checked right before deletion.
                if self.backup_root is not None:
                    self.verify_each([(row["files"][name], self.backup_place(name, row["files"][name]))
                                      for name in live if not self.backup_proven(name, row["files"][name])])
                if not rechecked:
                    # A retry after verification/crash obtains a new refreshed
                    # listing before deleting any remaining local preimage.
                    for name in live:
                        self.verify_file(row["files"][name])
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
                elif isinstance(exc, CD2Error):
                    # The health audit already consumes this safe string field;
                    # do not lose the cause after a unary verification failure.
                    row["reason"] = error_code(exc)
            self.save()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk-guard-only", action="store_true")
    parser.add_argument("--spool", type=Path, default=identity.SPOOL_ROOT / identity.ROOM)
    parser.add_argument("--state-path", type=Path, default=identity.STATE_DIR / "adapter-state.json")
    parser.add_argument("--archive-state", type=Path, default=identity.ARCHIVE_STATE)
    parser.add_argument("--cd2-endpoint", default="http://127.0.0.1:19798")
    parser.add_argument("--cd2-token-file", type=Path, default=Path("/opt/bilive/recording/cd2-token"))
    parser.add_argument("--lane-root", type=Path, default=None,
                        help="clip lane copy of this room (oci3_lane_spool_copy.sh); what it lacks is handed to it on eviction")
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
        observations = UploadObservations(client, args.archive_state.with_name("cd2-uploads.json"),
                                          roots=(identity.ARCHIVE_ROOT, identity.BACKUP_ROOT))
        threading.Thread(target=observations.watch, daemon=True).start()
        mover = Mover(spool=args.spool, archive_root=identity.ARCHIVE_ROOT,
                      state_path=args.state_path, archive_path=args.archive_state,
                      client=client, observations=observations,
                      backup_root=identity.BACKUP_ROOT, lane_root=args.lane_root)
        last_capacity_check = 0.0
        while True:
            if time.time() - last_capacity_check >= 60:
                disk_guard(args.spool)
                last_capacity_check = time.time()
            try:
                # Watch starts first to catch rapid Finished events, but its
                # readiness is not a prerequisite for create-only copying.
                # Missed events require upload_list evidence or retain local.
                mover.tick()
            except Exception as exc:
                print(f"SPOOL_RETRY error={type(exc).__name__}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
