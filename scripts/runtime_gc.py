#!/usr/bin/env python3
"""Bounded runtime inventory and collection of completed alignment scratch PCM.

Consumes the producer's INPUT/PROCESS/RESULT receipts. Never collects source
recordings, delivery media, context caches, reports, or arbitrary old files.
Run without --apply to prove a bounded set without deleting it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import time
import tempfile

PCM_NAMES = {"outer-8k-stereo.f32le": "outer", "source-8k-stereo.f32le": "source"}
ROOTS = ("out", "reports", "private-runs", "candidates", "sources", "cache")
PRODUCERS = {
    "source-window": "src/autoslice/song_lane.py:song_window_media_path",
    "context-cache": "src/autoslice/source_context_executor.py",
    "alignment-pcm": "src/autoslice/nested_source_audio_alignment.py",
    "piece-padding": "src/autoslice/producer_source_media.py:materialize / stitch",
    "delivery-media": "producer / subtitle burn / review and delivery",
    "other": "owner must be identified; directory is not a retirement receipt",
}


class Keep(ValueError):
    pass


def check_time(deadline):
    if time.monotonic() >= deadline:
        raise Keep("pass time budget exhausted")


def metadata(s):
    return dict(
        zip(
            ("dev", "ino", "mode", "bytes", "mtime_ns", "ctime_ns", "uid", "gid", "nlink"),
            (
                s.st_dev,
                s.st_ino,
                s.st_mode,
                s.st_size,
                s.st_mtime_ns,
                s.st_ctime_ns,
                s.st_uid,
                s.st_gid,
                s.st_nlink,
            ),
        )
    )


@contextmanager
def regular(path, *, single=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise Keep("absolute no-traversal path required")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for part in path.parent.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = nxt
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or (single and s.st_nlink != 1):
            raise Keep("requires independent regular file")
        yield fd, parent, path.name
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def snapshot(opened, deadline, *, limit=None):
    fd, parent, name = opened
    before = os.fstat(fd)
    if limit is not None and before.st_size > limit:
        raise Keep("receipt exceeds read limit")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    chunks = [] if limit is not None else None
    while True:
        check_time(deadline)
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    verify_identity(opened, metadata(before))
    return {**metadata(before), "sha256": "sha256:" + digest.hexdigest()}, (
        b"".join(chunks) if chunks is not None else None
    )


def verify_identity(opened, expected):
    fd, parent, name = opened
    actual = metadata(os.fstat(fd))
    if (
        actual != {k: expected[k] for k in actual}
        or metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != actual
    ):
        raise Keep("file or pathname changed")


def json_receipt(stack, path, deadline, proofs):
    opened = stack.enter_context(regular(path, single=True))
    image, data = snapshot(opened, deadline, limit=2 << 20)
    proofs.append((opened, image))
    value = json.loads(data)
    if not isinstance(value, dict):
        raise Keep("receipt must be an object")
    return value, image


def canonical_sha(value):
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def active_job(job, target_identity):
    """Root-only Linux census; incomplete visibility must fail closed."""
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            cmd = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            cwd = os.readlink(process / "cwd")
            if str(job.parent) in cmd or cwd == str(job) or cwd.startswith(str(job) + "/"):
                raise Keep("active process owns this alignment namespace")
            for fd in (process / "fd").iterdir():
                try:
                    s = fd.stat()
                    if (s.st_dev, s.st_ino) == target_identity:
                        raise Keep("scratch file is open")
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise Keep("process visibility incomplete") from exc


def decode_sha(source_fd, deadline, max_bytes):
    # Do not execute argv from a receipt. Use the sole recognized producer recipe.
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        f"{'/proc/self/fd' if Path('/proc/self/fd').exists() else '/dev/fd'}/{source_fd}",
        "-map",
        "0:a:0",
        "-ar",
        "8000",
        "-ac",
        "2",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    os.lseek(source_fd, 0, os.SEEK_SET)
    process = subprocess.Popen(
        argv, pass_fds=(source_fd,), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    digest, total = hashlib.sha256(), 0
    try:
        os.set_blocking(process.stdout.fileno(), False)
        while True:
            check_time(deadline)
            if not select.select(
                [process.stdout], [], [], min(0.5, max(0, deadline - time.monotonic()))
            )[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise Keep("regenerated PCM exceeds declared bytes")
            digest.update(chunk)
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise Keep("regeneration failed")
        return "sha256:" + digest.hexdigest(), total, argv
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def collect_pcm(path, base, state, deadline, *, apply=False, busy=active_job, decoder=decode_sha):
    """Retire one exact producer-owned scratch, retaining all evidence and sources."""
    path, base = Path(path), Path(base)
    if not path.is_relative_to(base / "private-runs") or path.name not in PCM_NAMES:
        raise Keep("not in the automatic scratch class and namespace")
    job, role = path.parent, PCM_NAMES[path.name]
    with ExitStack() as stack:
        proofs = []
        # Existing producer and collector share this exact flock inode.
        lock = stack.enter_context(regular(job.parent / "alignment.lock", single=True))
        fcntl.flock(lock[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_image = metadata(os.fstat(lock[0]))
        proofs.append((lock, lock_image))
        request, _ = json_receipt(stack, job / "INPUT.json", deadline, proofs)
        process, _ = json_receipt(stack, job / "PROCESS.json", deadline, proofs)
        result, result_image = json_receipt(stack, job / "RESULT.json", deadline, proofs)
        request_sha = canonical_sha(request)
        if (
            request.get("schema_version") != "nested-source-audio-alignment-request.v1"
            or process.get("schema_version") != "nested-source-audio-alignment-process.v1"
            or result.get("schema_version") != "nested-media-source-audio-alignment-result.v1"
            or process.get("status") != "COMPLETE"
            or process.get("result_path") != str(job / "RESULT.json")
            or process.get("result_sha256") != result_image["sha256"]
            or process.get("request_sha256") != request_sha
            or result.get("request_sha256") != request_sha
            or job.name != request_sha.removeprefix("sha256:")
        ):
            raise Keep("producer completion/binding not proved")
        if result.get("mapping_receipt"):
            ref = result["mapping_receipt"]
            if ref.get("path") != str(job / "mapping-receipt.json"):
                raise Keep("unexpected mapping receipt path")
            _, mapping_image = json_receipt(stack, job / "mapping-receipt.json", deadline, proofs)
            if mapping_image["sha256"] != ref.get("sha256"):
                raise Keep("mapping receipt drift")
        extraction = result["alignment_evidence"]["extraction"]
        pcm, media = extraction[role + "_pcm"], request[role + "_media"]
        if (
            pcm.get("path") != str(path)
            or pcm.get("returncode") != 0
            or pcm.get("sample_rate_hz") != 8000
            or pcm.get("channels") != 2
            or extraction[role + "_media"] != media
        ):
            raise Keep("unrecognized PCM provenance")
        source_path = Path(media["path"])
        # FUSE/cloud and temporary RAM inputs require a separate persistence proof.
        if not source_path.is_relative_to(base) or "CloudFS" in source_path.parts:
            raise Keep("retained local source is outside pipeline namespace")
        source = stack.enter_context(regular(source_path))
        source_image, _ = snapshot(source, deadline)
        if (
            source_image["dev"] != os.stat(base).st_dev
            or source_image["sha256"] != media["sha256"]
            or source_image["bytes"] != media["bytes"]
        ):
            raise Keep("retained source drift")
        target = stack.enter_context(regular(path, single=True))
        image, _ = snapshot(target, deadline)
        if (
            image["dev"] != os.stat(base).st_dev
            or image["sha256"] != pcm["sha256"]
            or image["bytes"] != pcm["bytes"]
        ):
            raise Keep("scratch hash/size/device drift")
        busy(job, (image["dev"], image["ino"]))
        regen_sha, regen_bytes, _ = decoder(source[0], deadline, image["bytes"])
        if regen_sha != image["sha256"] or regen_bytes != image["bytes"]:
            raise Keep("current decoder cannot reconstruct exact scratch bytes")
        # Rehash after decoding; don't rely on just size or old source metadata.
        if snapshot(source, deadline)[0] != source_image or snapshot(target, deadline)[0] != image:
            raise Keep("media changed during recovery proof")
        busy(job, (image["dev"], image["ino"]))
        for opened, expected in proofs:
            verify_identity(opened, expected)
        record = {
            "schema_version": "runtime-gc-recovery.v1",
            "class": "alignment-pcm",
            "path": str(path),
            "preimage": image,
            "source": {"path": str(source_path), **source_image},
            "recipe": "ffmpeg -i SOURCE -map 0:a:0 -ar 8000 -ac 2 -c:a pcm_f32le -f f32le OUTPUT",
            "producer_result": {"path": str(job / "RESULT.json"), "sha256": result_image["sha256"]},
            "status": "PROVED_ONLY",
            "allocated_bytes": os.fstat(target[0]).st_blocks * 512,
        }
        if apply:
            # Durable before unlink; original inode/ctime are not restorable.
            recovery_root = Path(state) / "recoveries"
            if (
                recovery_root.exists()
                and sum(p.lstat().st_size for p in recovery_root.iterdir()) > 32 << 20
            ):
                raise Keep("recovery ledger reached 32 MiB; archive it before more collection")
            key = canonical_sha({"path": str(path), "preimage": image}).removeprefix("sha256:")
            receipt_path = Path(state) / "recoveries" / (key + ".json")
            record["status"] = "PREPARED"
            write_json(receipt_path, record)
            check_time(deadline)
            verify_identity(target, image)
            os.unlink(target[2], dir_fd=target[1])
            record["status"] = "REMOVED"
            record["removed_at_utc"] = now()
            try:
                os.fsync(target[1])
                write_json(receipt_path, record)
            except OSError as exc:
                # PREPARED remains durable. Report the removed subset honestly;
                # an acknowledgement failure must never turn it into "kept".
                record["receipt_update_error"] = str(exc)
        return record


def classify(path):
    if path.name in PCM_NAMES:
        return "alignment-pcm"
    if ".context_clip_cache" in path.parts:
        return "context-cache"
    if path.name.endswith("_source.mp4"):
        return "source-window"
    if path.name.startswith(("piece_", "padded_")) and path.suffix == ".mp4":
        return "piece-padding"
    if path.suffix.lower() in (".mp4", ".mkv", ".flv"):
        return "delivery-media"
    return "other"


def inventory(base, deadline):
    seen, rows, pcm, errors = set(), {}, [], []
    device = os.stat(base).st_dev
    for name in ROOTS:
        root = base / name
        if not root.exists():
            continue
        if root.is_symlink():
            errors.append(str(root) + ": symlink root refused")
            continue
        for directory, dirs, files in os.walk(
            root, followlinks=False, onerror=lambda e: errors.append(str(e))
        ):
            check_time(deadline)
            dirs[:] = [
                d
                for d in dirs
                if not (Path(directory) / d).is_symlink()
                and (Path(directory) / d).stat().st_dev == device
            ]
            for filename in files:
                path = Path(directory) / filename
                try:
                    s = path.lstat()
                    if not stat.S_ISREG(s.st_mode) or s.st_dev != device:
                        continue
                    key = (s.st_dev, s.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    kind = classify(path)
                    row = rows.setdefault(
                        name + "/" + kind,
                        {"allocated_bytes": 0, "files": 0, "producer_hint": PRODUCERS[kind]},
                    )
                    row["allocated_bytes"] += s.st_blocks * 512
                    row["files"] += 1
                    if name == "private-runs" and filename in PCM_NAMES:
                        pcm.append((s.st_size, str(path)))
                except OSError as exc:
                    errors.append(str(exc))
    return rows, sorted(pcm, reverse=True), errors


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/opt/bilive/autoslice"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/autoslice-runtime-gc"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=90)
    parser.add_argument("--max-files", type=int, default=8)
    parser.add_argument("--max-reclaim-bytes", type=int, default=512 << 20)
    args = parser.parse_args()
    if min(args.max_seconds, args.max_files, args.max_reclaim_bytes) <= 0:
        parser.error("budgets must be positive")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = args.state_dir / "gc.lock"
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    os.close(fd)
    with regular(lock_path, single=True) as lock:
        fd = lock[0]
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("runtime GC already running")
            return 0
        deadline, before = (
            time.monotonic() + args.max_seconds,
            os.statvfs(args.base).f_bavail * os.statvfs(args.base).f_frsize,
        )
        report = {
            "schema_version": "runtime-gc-pass.v1",
            "time_utc": now(),
            "apply": args.apply,
            "removed": [],
            "proved": [],
            "kept": [],
            "errors": [],
            "out_media": "requires existing quiet/reference/terminal/source/approved-set gates; automatic deletion disabled",
            "before_free_bytes": before,
        }
        budget = 0
        budget_skips = 0
        try:
            report["inventory"], candidates, report["errors"] = inventory(args.base, deadline)
            report["pcm_candidates"] = len(candidates)
            if not args.inventory_only and not report["errors"]:
                for size, path in candidates:
                    if (
                        len(report["removed"]) + len(report["proved"]) >= args.max_files
                        or budget + size > args.max_reclaim_bytes
                    ):
                        budget_skips += 1
                        continue
                    try:
                        record = collect_pcm(
                            path, args.base, args.state_dir, deadline, apply=args.apply
                        )
                        budget += size
                        report["removed" if args.apply else "proved"].append(record)
                        if record.get("receipt_update_error"):
                            report["errors"].append(
                                f"removed {path}; receipt update failed: {record['receipt_update_error']}"
                            )
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        subprocess.TimeoutExpired,
                    ) as exc:
                        if len(report["kept"]) < 30:
                            report["kept"].append({"path": path, "reason": str(exc)})
                    if time.monotonic() >= deadline:
                        break
        except (OSError, ValueError) as exc:
            report["errors"].append(str(exc))
        report["removed_files"] = len(report["removed"])
        report["proved_files"] = len(report["proved"])
        report["unprocessed_by_budget"] = budget_skips
        report["inventory_basis"] = (
            "live interval; regular file allocated blocks; unique device+inode; producer hints are not provenance proofs"
        )
        report["removed_allocated_bytes"] = sum(r["allocated_bytes"] for r in report["removed"])
        report["after_free_bytes"] = os.statvfs(args.base).f_bavail * os.statvfs(args.base).f_frsize
        report["measured_free_delta_bytes"] = report["after_free_bytes"] - before
        report["elapsed_seconds"] = round(args.max_seconds - (deadline - time.monotonic()), 3)
        write_json(args.state_dir / "last.json", report)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in ("inventory", "removed", "proved", "kept")
                },
                ensure_ascii=False,
            )
        )
        return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
