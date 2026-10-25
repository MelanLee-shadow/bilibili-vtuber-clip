#!/usr/bin/env python3
"""Plan a free capacity cleanup. Never deletes — emits a gated plan.

维护者 approved this gate after the ENOSPC pass deleted
`out/2026-07-18/*/song_selector_full/**/*.mp4` without scanning the ledger and
left a dangling authority reference in state (the mis-delete again,
one layer up). Nothing gets deleted from `out/` until it clears all four gates.

    GATE 0  quiet window   — DISABLED present, no runner/ffmpeg/produce process,
                             runner.lock free. Live production is an abort, not
                             a warning: the runner writes into out/<today>.
    GATE 1  class allowlist— only classes with committed precedent. Anything
                             unclassified is KEPT. `.recut.mp4` and
                             `.burned-final-*` are never in the allowlist: that
                             is exactly what 07-30 destroyed.
    GATE 2  authority cite — no Tier A file may reference the path. `out/` is
                             not Tier A (an intermediate cited only by its own
                             sibling manifest is self-referential), and a cited
                             *directory* protects only when specific enough to
                             name a candidate — `out/<day>` alone is a location
                             mention that would otherwise shield a whole day.
    GATE 3  owner terminal — the owning candidate must be in a terminal state,
                             and must not sit in pending_talk / pending_song.

A per-day CloudFS presence check (regenerability) is reported but must be
confirmed by the operator: a hung FUSE mount can make present sources look
absent, and that misread would authorize deleting the irreplaceable.

    python3 scripts/cleanup_preflight_scan.py [--base ...] [--json plan.json]
                                              [--allow-source-extractions]
                                              [--ignore-quiet-window]
"""
from __future__ import annotations

import argparse
import collections
from datetime import date as calendar_date
import glob
import hashlib
import json
import os
import re
import subprocess
import stat
import sys
from pathlib import Path

from _cleanup_appledouble import metadata_reference_text
from _cleanup_file_identity import capture_preimage

TEXT_EXT = {".json", ".jsonl", ".md", ".txt", ".srt", ".ass", ".log",
            ".py", ".sh", ".lrc", ".yaml", ".yml", ".csv"}
MEDIA_EXT = {".mp4", ".m4s", ".mp3", ".wav", ".flac", ".m4a", ".mkv", ".ts", ".aac"}

# Tier A authority. `out/` is deliberately absent — see GATE 2.
TIER_A = ["state", "assets", "profiles", "src", "scripts", "docs", "cache",
          "forensics", "evals", "review_jobs", "review_packages", "upload_staging",
          "manual_review", "recovery", "quarantine", "repo", "reports"]

# These stores have live capabilities as well as historical receipts. Read
# them in the same stable scan, but do not cite completed source intermediates
# forever just because an immutable prepared manifest still names them.
RECOVERY_STORES = {".prepared-deliveries", ".delivery-journal", ".producer-batch-journal"}

# Classes with committed precedent (cleanup_manifests/free_autoslice_*_2026073{0}
# and _20260810). Regenerable from retained source recordings.
ALLOWED_CLASSES = {
    "source-context.context.mp4",
    "padded_N.mp4",
    "piece_N.mp4",
    "input.complete-audio.mp3",
    "working.wav",
}
# Defensible extension of the same regenerability logic, but a NEW class:
# opt in explicitly and the manifest must say so.
SOURCE_EXTRACTION_CLASSES = {"_source.mp4", "_full_source.mp4"}

TERMINAL_STATES = {"published", "candidate_rejected", "failed", "superseded",
                   "blocked", "boundary_unrepairable", "delivery_quarantined"}

CANDIDATE_ID = re.compile(r"(auto_\d+_\d+_\d+|song_\d+_\d+|seededsong_\d+_\d+)")
OwnerIdentity = tuple[str, str, str]  # absolute runtime root, recording date, CID


def _owner_identity(base: str, day: str, cid: str) -> OwnerIdentity:
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("cleanup owner date is invalid")
    calendar_date.fromisoformat(day)
    if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cid):
        raise ValueError("cleanup owner candidate ID is invalid")
    return os.path.abspath(base), day, cid


def owning_identity(base: str, path: str) -> OwnerIdentity:
    """Bind a target to its actual out/<date>/<CID>/... directory, not text."""
    root = Path(os.path.abspath(base))
    target = Path(path)
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError("cleanup owner path is not a bounded absolute path")
    try:
        parts = target.relative_to(root / "out").parts
    except ValueError as exc:
        raise ValueError("cleanup owner path is outside this runtime's out namespace") from exc
    if len(parts) < 3 or not CANDIDATE_ID.fullmatch(parts[1]):
        raise ValueError("cleanup owner path lacks out/<date>/<CID>/file identity")
    return _owner_identity(base, parts[0], parts[1])


def owner_claim(identity: OwnerIdentity) -> dict[str, str]:
    root, day, cid = identity
    return {"runtime_root": root, "date": day, "candidate_id": cid}


OUT_PATH = re.compile(r"/opt/bilive/autoslice/out/[^\"'\s,\]\}<>()]+")


def classify(name: str) -> str:
    for suffix in ("source-context.context.mp4", "input.complete-audio.mp3",
                   "_full_source.mp4", "_source.mp4", ".burned-final-sapphire72.mp4",
                   ".burned-final-speaker.mp4", ".recut.mp4"):
        if name.endswith(suffix):
            return suffix
    if re.match(r"^padded_\d+\.mp4$", name):
        return "padded_N.mp4"
    if re.match(r"^piece_\d+\.mp4$", name):
        return "piece_N.mp4"
    if name.endswith(".wav"):
        return "working.wav"
    return "(unclassified)" + os.path.splitext(name)[1].lower()


def _own_process_ancestry() -> set[int]:
    """Every pid from here up to init, so we can recognise our own flock."""
    ancestry: set[int] = set()
    pid = os.getpid()
    while pid and pid != 1:
        ancestry.add(pid)
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                ppid = next(line for line in fh if line.startswith("PPid:"))
            pid = int(ppid.split()[1])
        except (OSError, StopIteration, ValueError):
            break
    return ancestry


_PRODUCTION_SCRIPTS = {"session_autoslice.py", "produce_slice_package.py"}
_PYTHON_PROCESS = re.compile(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?(?:-dbg)?")
_LAUNCH_CODE = re.compile(r"session_autoslice|produce_slice_package|ffmpeg")
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
_WRAPPERS = {"env", "flock", "timeout"}


def _production_role(name: str, argv: list[str] | None = None, depth: int = 0) -> str | None:
    """Identify execution positions, never ordinary configuration/data arguments.

    Shell/Python launch code remains conservative. This is the existing named
    production guard, not proof against arbitrary launchers or process spoofing.
    """
    if depth > 4 or not name:
        raise ValueError("unknown process identity")
    name = Path(name).name
    if re.fullmatch(r"ffmpeg(?:[-._0-9].*)?", name):
        return "ffmpeg"
    if name in _PRODUCTION_SCRIPTS:
        return name
    python = bool(_PYTHON_PROCESS.fullmatch(name))
    if not (python or name in _SHELLS | _WRAPPERS):
        return None
    if not argv:
        raise ValueError("unreadable execution arguments")
    index = 1
    if python:
        while index < len(argv):
            token = argv[index]
            if token == "--":
                index += 1
                break
            launch = re.fullmatch(r"-[bBdEiIOPqsSuvVx]*([cm])(.*)", token)
            if launch:
                kind, entry = launch.groups()
                if not entry:
                    index += 1
                    if index >= len(argv):
                        raise ValueError("missing Python entry")
                    entry = argv[index]
                if kind == "c":
                    return "Python launch code" if _LAUNCH_CODE.search(entry) else None
                return entry.rsplit(".", 1)[-1] + ".py" if (
                    entry.rsplit(".", 1)[-1] + ".py" in _PRODUCTION_SCRIPTS) else None
            if token in {"-W", "-X", "--check-hash-based-pycs"}:
                index += 2
            elif token.startswith(("-W", "-X", "--check-hash-based-pycs=")):
                index += 1
            elif re.fullmatch(r"-[bBdEiIOPqsSuvVx]+", token):
                index += 1
            elif token.startswith("-") and token != "-":
                raise ValueError("unknown Python execution option")
            else:
                break
        if index >= len(argv) or argv[index] == "-":
            raise ValueError("unknown Python stdin/interactive entry")
        return _production_role(Path(argv[index]).name, depth=depth + 1)
    if name in _SHELLS:
        while index < len(argv):
            token = argv[index]
            if token == "--":
                index += 1
                break
            if token.startswith("-") and token != "-":
                if token in {"-o", "-O", "--rcfile", "--init-file"}:
                    if index + 1 >= len(argv):
                        raise ValueError("missing shell option value")
                    index += 2
                    continue
                if token.startswith("--"):
                    if token not in {"--noprofile", "--norc", "--posix", "--restricted",
                                     "--login", "--interactive", "--verbose", "--debugger"}:
                        raise ValueError("unknown shell execution option")
                    index += 1
                    continue
                if "c" in token[1:]:
                    if index + 1 >= len(argv):
                        raise ValueError("missing shell launch code")
                    return "shell launch code" if _LAUNCH_CODE.search(argv[index + 1]) else None
                if "s" in token[1:]:
                    raise ValueError("unknown shell stdin entry")
                if not re.fullmatch(r"-[abefhkmnptuvxBCEHPTilrD]+", token):
                    raise ValueError("unknown shell execution option")
                index += 1
            elif token.startswith("+"):
                raise ValueError("unknown shell execution option")
            else:
                break
        if index >= len(argv):
            return None  # A readable shell with no command/script entry is not named production.
        if argv[index] == "-":
            raise ValueError("unknown shell stdin entry")
        return _production_role(Path(argv[index]).name, depth=depth + 1)
    # Parse the common wrapper command position. An unrecognised option holds;
    # lock paths, environment assignments and command data are not executables.
    valued = {"env": {"-u", "--unset", "-C", "--chdir"},
              "flock": {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
              "timeout": {"-s", "--signal", "-k", "--kill-after"}}[name]
    flags = {"env": {"-i", "--ignore-environment", "-0", "--null"},
             "flock": {"-n", "--nonblock", "--nb", "-x", "--exclusive", "-s", "--shared",
                       "-u", "--unlock", "-o", "--close", "-F", "--no-fork", "--verbose"},
             "timeout": {"--preserve-status", "--foreground", "-v", "--verbose"}}[name]
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in valued:
            index += 2
        elif token in flags or any(token.startswith(option + "=") for option in valued):
            index += 1
        elif token.startswith("-"):
            raise ValueError("unknown launch wrapper option")
        elif name == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
        else:
            break
    if name in {"flock", "timeout"}:
        index += 1  # lock path/FD or timeout duration
    if name == "flock" and index < len(argv) and argv[index] in {"-c", "--command"}:
        if index + 1 >= len(argv):
            raise ValueError("missing flock launch code")
        return "flock launch code" if _LAUNCH_CODE.search(argv[index + 1]) else None
    if index >= len(argv):
        # `flock -n 9` only changes the inherited FD's lock; it launches nothing.
        if name == "flock" and index == len(argv):
            return None
        raise ValueError("missing launch wrapper command")
    return _production_role(argv[index], argv[index:], depth + 1)


def _process_argv(pid: int, expected_name: str) -> list[str]:
    """Only read argv for a recognised interpreter/wrapper, preserving NUL boundaries."""
    process = Path("/proc") / str(pid)
    if (process / "comm").read_text().rstrip("\n") != expected_name:
        raise ValueError("process identity changed during scan")
    with (process / "cmdline").open("rb") as stream:
        raw = stream.read(65537)
    if not raw or len(raw) > 65536 or not raw.endswith(b"\0"):
        raise ValueError("unreadable or oversized execution arguments")
    return [part.decode("utf-8") for part in raw[:-1].split(b"\0")]


def _quiet_process_problems() -> list[str]:
    """Scan public names first; missing scan/necessary entry data is never CLEAR."""
    if sys.platform != "linux":
        return ["production process scan unavailable: Linux structured process data required"]
    try:
        scanned = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True,
                                 text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ["production process scan failed"]
    if scanned.returncode != 0 or not scanned.stdout.strip():
        return ["production process scan failed or returned no identities"]
    problems, seen = [], set()
    for line in scanned.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if (len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) <= 0
                or int(fields[0]) in seen):
            return ["production process scan returned malformed identities"]
        pid, name = int(fields[0]), fields[1]
        seen.add(pid)
        try:
            argv = None
            if (_PYTHON_PROCESS.fullmatch(name) or name in _SHELLS | _WRAPPERS
                    or name in {entry[:15] for entry in _PRODUCTION_SCRIPTS}):
                argv = _process_argv(pid, name)
                entry_name = Path(argv[0]).name
                if entry_name.startswith("-") and entry_name[1:] in _SHELLS:
                    entry_name = entry_name[1:]
                if (not _PYTHON_PROCESS.fullmatch(entry_name)
                        and entry_name not in _SHELLS | _WRAPPERS | _PRODUCTION_SCRIPTS
                        and not re.fullmatch(r"ffmpeg(?:[-._0-9].*)?", entry_name)):
                    raise ValueError("unknown interpreter/wrapper identity")
                name = entry_name
            role = _production_role(name, argv)
            if role:
                problems.append(f"in-flight production process: PID {pid} ({role})")
        except FileNotFoundError:
            try:
                (Path("/proc") / str(pid)).stat()
            except FileNotFoundError:
                continue  # The listed process exited; do not confuse a read error with exit.
            except OSError:
                pass
            problems.append(f"production process identity unreadable: PID {pid}")
        except (OSError, UnicodeError, ValueError):
            problems.append(f"production process identity unreadable: PID {pid}")
    return problems


def quiet_window(base: str) -> list[str]:
    """Reasons the host is NOT quiet. Empty list means safe to proceed."""
    problems = []
    if not os.path.exists(f"{base}/DISABLED"):
        problems.append("DISABLED flag absent — the cron runner can start a tick at any moment")
    problems.extend(_quiet_process_problems())
    lock = f"{base}/runner.lock"
    if os.path.exists(lock):
        held = subprocess.run(["fuser", lock], capture_output=True, text=True)
        holders = {int(tok) for tok in held.stdout.split() if tok.isdigit()}
        ancestry = _own_process_ancestry()
        # Running under `flock runner.lock` is the *intended* way to delete:
        # taking the lock is what stops the next cron tick from starting.
        # fuser reports everyone with the file *open*, which is not the same as
        # holding the lock — a stale reader shows up there too. flock is
        # exclusive, so once an ancestor of ours holds it nobody else can, and
        # the remaining open handles are noise. Only when we do NOT hold it
        # does a foreign handle mean someone else's tick (or, as on,
        # an orphaned sentinel that sat on the lock for seven hours).
        foreign = holders - ancestry
        if foreign and not (holders & ancestry):
            problems.append(f"runner.lock is held by another process: {sorted(foreign)}")
    return problems


# Allow the observed 45,469,780-byte archived authority inventory to be
# scanned completely. Keep a finite cap; larger inputs still fail closed.
MAX_AUTHORITY_BYTES = 64 * 2**20


class AuthorityScanError(ValueError):
    """An incomplete authority scan cannot establish that a target is unreferenced."""


def _scan_identity(info: os.stat_result) -> tuple[int, ...]:
    # Ignore read-induced atime, but retain namespace/content/permission identity.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_uid, info.st_gid)


def _authority_text(path: str, expected: os.stat_result) -> tuple[str, str | None]:
    """Read bounded stable bytes; return text and an optional metadata companion."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode)
                or _scan_identity(before) != _scan_identity(expected)):
            raise AuthorityScanError(f"authority file is unsafe or changed: {path}")
        if before.st_size > MAX_AUTHORITY_BYTES:
            raise AuthorityScanError(f"authority file exceeds scan limit: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, MAX_AUTHORITY_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_AUTHORITY_BYTES:
                raise AuthorityScanError(f"authority file exceeds scan limit: {path}")
        after = os.fstat(fd)
        if total != before.st_size or _scan_identity(after) != _scan_identity(before):
            raise AuthorityScanError(f"authority file changed during scan: {path}")
        raw = b"".join(chunks)
        try:
            metadata = metadata_reference_text(raw, os.path.basename(path))
        except ValueError as exc:
            raise AuthorityScanError(f"unsupported AppleDouble authority sidecar: {path}: {exc}") from exc
        if metadata is not None:
            return metadata
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError as exc:
            # Include only the locator, never private document contents.
            raise AuthorityScanError(f"authority file is not valid UTF-8: {path}") from exc
    finally:
        os.close(fd)


def _prepared_recovery_references(
    base: str, documents: dict[str, str], directories: set[str], required: set[str],
    observe_file,
) -> set[str]:
    """Protect resumable producer inputs, using the actual read-only consumers.

    A terminal state row does not retire a private handle. Only a validated
    COMMITTED journal with its exact installed target inodes/bytes releases
    the handle's original inputs. Pending journals win over completed ones.
    No journal, target, state or checkpoint is created by these checks.
    """
    if not documents and not directories and not required:
        return set()
    checkout = str(Path(__file__).resolve().parents[1])
    if checkout not in sys.path:
        sys.path.insert(0, checkout)
    from src.autoslice import producer_batch_transaction as batch
    from src.autoslice import producer_delivery_transaction as delivery
    from src.autoslice.producer_batch_projection import prepared_batch_entry
    from src.autoslice.deployed_code_root import deployed_code_root

    runtime = Path(base)
    for directory in directories:
        path = Path(directory)
        if stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
            raise AuthorityScanError(f"producer recovery namespace mode is unsafe: {path}")
        parts = path.relative_to(runtime).parts
        if parts[0] == ".prepared-deliveries":
            if len(parts) > 4:
                raise AuthorityScanError(f"producer recovery namespace is unsafe: {path}")
            if len(parts) == 4 and str(path / "prepared.json") not in documents:
                raise AuthorityScanError(f"producer recovery manifest is missing: {path}")
    for path in required:
        if path not in documents:
            raise AuthorityScanError(f"referenced producer recovery document is missing: {path}")

    handles = {}
    prepared = {}
    checkpoints = {}
    for name, text in documents.items():
        path = Path(name)
        parts = path.relative_to(runtime).parts
        if parts[0] != ".prepared-deliveries":
            continue
        if len(parts) != 5:
            raise AuthorityScanError(f"producer recovery document is unsafe: {path}")
        if path.name == "prepared.json":
            handle = delivery.load_prepared_delivery(
                runtime_root=runtime, manifest_path=path, allow_materialized=True,
            )
            handles[name] = handle
            prepared[name] = json.loads(text)
        elif re.fullmatch(r"dispatch-result-[0-9a-f]{64}\.json", path.name):
            checkpoints[name] = json.loads(text)
        else:
            raise AuthorityScanError(f"unknown producer recovery document: {path}")

    pending: set[str] = set()
    completed: set[str] = set()
    refs: set[str] = set()

    def installed_targets(document: dict) -> None:
        # Completion is about the installed private inode, not just a status
        # or matching bytes at a foreign delivery target. Sources may already
        # have been retired by an earlier reviewed cleanup.
        for artifact in document["artifacts"]:
            target = Path(artifact["target_path"])
            observe_file(str(target))
            snapshot = delivery._regular_snapshot(target)
            if (snapshot["device"], snapshot["inode"], snapshot["sha256"]) != (
                artifact["staged_device"], artifact["staged_inode"],
                artifact["staged_sha256"].removeprefix("sha256:"),
            ):
                raise AuthorityScanError(f"committed producer target drifts: {target}")

    for name in documents:
        path = Path(name)
        parts = path.relative_to(runtime).parts
        if parts[0] == ".prepared-deliveries":
            continue
        if len(parts) != 2 or path.suffix != ".json":
            raise AuthorityScanError(f"producer journal path is unsafe: {path}")
        if parts[0] == ".producer-batch-journal":
            journal = batch._read_journal(path)
            batch._validate_journal_binding(runtime_root=runtime, path=path, document=journal)
            entries = batch._entries_from_journal(runtime, journal)
            if journal["status"] == "COMMITTED":
                batch.validate_committed_batch_journal_for_deploy(runtime_root=runtime, path=path)
        else:
            raw = json.loads(documents[name])
            manifest = raw.get("prepared_manifest") if isinstance(raw, dict) else None
            if manifest not in handles:
                raise AuthorityScanError(f"producer journal manifest is missing: {path}")
            handle = handles[manifest]
            expected = runtime / ".delivery-journal" / f"{handle.lane}-{handle.candidate_id}-{handle.prepared_sha256}.json"
            if path != expected:
                raise AuthorityScanError(f"producer journal filename drifts: {path}")
            journal = delivery._read_journal(path, handle=handle, artifacts=prepared[manifest]["artifacts"])
            entries = [batch.PreparedBatchEntry(handle.lane, handle.candidate_id, handle)]
        for entry in entries:
            manifest = str(entry.handle.manifest_path)
            if handles.get(manifest) != entry.handle:
                raise AuthorityScanError(f"producer journal handle is missing or drifts: {path}")
            if journal["status"] == "COMMITTED":
                installed_targets(prepared[manifest])
                completed.add(manifest)
            else:
                pending.add(manifest)

    checkpoint_groups = collections.defaultdict(dict)
    for name, body in checkpoints.items():
        path = Path(name)
        manifest = str(path.parent / "prepared.json")
        handle = handles.get(manifest)
        if handle is None or not isinstance(body, dict):
            raise AuthorityScanError(f"producer checkpoint manifest is missing: {path}")
        unsigned = dict(body)
        digest = unsigned.pop("checkpoint_sha256", None)
        binding = body.get("binding")
        result = body.get("result")
        canonical = delivery._canonical_bytes
        fields = {"schema_version", "binding", "result", "source_dependencies", "prepared_sha256", "upload_enabled", "checkpoint_sha256"}
        if body.get("schema_version") == "producer-result-checkpoint.v2":
            # A's current v2 names the frozen full deployed manifest. The
            # earlier private v2 draft is readable as history; an active
            # checkpoint must still pass the actual leaf's stricter schema.
            authority_field = ("deployed_authority_manifest_b64"
                               if "deployed_authority_manifest_b64" in body
                               else "consumer_authority_manifest_b64")
            fields.update({authority_field, "reprepared_from"})
        if (set(body) != fields
                or body.get("schema_version") not in {"producer-result-checkpoint.v1", "producer-result-checkpoint.v2"}
                or stat.S_IMODE(os.lstat(path).st_mode) != 0o600
                or not isinstance(binding, dict) or not isinstance(result, dict)
                or body.get("upload_enabled") is not False
                or body.get("prepared_sha256") != "sha256:" + handle.prepared_sha256
                or digest != "sha256:" + hashlib.sha256(canonical(unsigned)).hexdigest()
                or path.name != "dispatch-result-" + hashlib.sha256(canonical(binding)).hexdigest() + ".json"
                or not isinstance(body.get("source_dependencies"), dict)
                or prepared_batch_entry(runtime_root=runtime, lane=handle.lane,
                                        candidate_id=handle.candidate_id, result=result)
                != batch.PreparedBatchEntry(handle.lane, handle.candidate_id, handle)):
            raise AuthorityScanError(f"producer checkpoint authority drifts: {path}")
        checkpoint_groups[(handle.lane, handle.candidate_id, path.name)][manifest] = body

    # v2 retains exact predecessor receipts across equivalent re-prepares.
    # Validate that same single chain without asking the public loader to
    # mint a current handle. A journaled leaf retires its old dispatch chain.
    predecessors: set[str] = set()
    journaled_history = completed | pending
    for group in checkpoint_groups.values():
        parent_of = {}
        for manifest, body in group.items():
            previous = body.get("reprepared_from")
            if previous is None:
                continue
            if not isinstance(previous, dict) or set(previous) != {"manifest_path", "prepared_sha256", "checkpoint_sha256"}:
                raise AuthorityScanError("producer checkpoint predecessor is invalid")
            parent = previous["manifest_path"]
            if parent not in group or previous != {
                "manifest_path": parent, "prepared_sha256": "sha256:" + handles[parent].prepared_sha256,
                "checkpoint_sha256": group[parent]["checkpoint_sha256"],
            }:
                raise AuthorityScanError("producer checkpoint predecessor drifts")
            parent_of[manifest] = parent
        parents = set(parent_of.values())
        leaves = set(group) - parents
        if len(leaves) != 1:
            raise AuthorityScanError("producer checkpoint namespace is ambiguous")
        leaf = next(iter(leaves))
        visited = set()
        cursor = leaf
        while cursor not in visited:
            visited.add(cursor)
            if cursor not in parent_of:
                break
            cursor = parent_of[cursor]
        else:
            raise AuthorityScanError("producer checkpoint lineage cycles")
        if visited != set(group):
            raise AuthorityScanError("producer checkpoint lineage is disconnected")
        predecessors.update(parents)
        if leaf in journaled_history:
            journaled_history.update(parents)

    for manifest, handle in handles.items():
        document = prepared[manifest]
        # Formal targets remain cited after original inputs are retired.
        refs.update(row["target_path"] for row in document["artifacts"])
        if manifest in journaled_history and manifest not in pending:
            continue
        allow_installed = manifest in pending
        for artifact in document["artifacts"]:
            observe_file(artifact["source_path"])
            observe_file(artifact["staged_path"], may_be_missing=True)
            observe_file(artifact["target_path"], may_be_missing=True)
        delivery._read_document(handle, allow_materialized=allow_installed)
        repo = deployed_code_root(runtime)
        observe_file(str(repo / "DEPLOYED_COMMIT"))
        observe_file(str(repo / "DEPLOYED_AUTHORITY_MANIFEST.json"))
        if (manifest not in predecessors
                and document["deployed_authority"] != delivery.deployment_authority_binding(runtime)):
            raise AuthorityScanError(f"producer recovery deployment drifts: {handle.manifest_path}")
        delivery._checked_artifacts(document, runtime_root=runtime, allow_installed=allow_installed)
        refs.update(row["source_path"] for row in document["artifacts"])

    for name, body in checkpoints.items():
        path = Path(name)
        manifest = str(path.parent / "prepared.json")
        handle = handles[manifest]
        if manifest in journaled_history:
            # Once a formal journal exists, the ordinary runner resumes that
            # journal before dispatch. It needs artifact sources, not the
            # pre-callback spec/input checkpoint (which may be materialized).
            continue
        loader = getattr(batch, "_load_result_checkpoint", None)
        legacy_loader = getattr(batch, "load_prepared_result_checkpoint", None)
        if loader is None and legacy_loader is None:
            raise AuthorityScanError(f"producer checkpoint consumer is unavailable: {path}")
        for source, snapshot in body["source_dependencies"].items():
            if not isinstance(source, str) or not isinstance(snapshot, dict):
                raise AuthorityScanError(f"producer checkpoint source is invalid: {path}")
            observe_file(source, may_be_missing=snapshot == {"missing": True})
            resolved = snapshot.get("resolved_path")
            if isinstance(resolved, str) and resolved != source:
                observe_file(resolved)
                refs.add(resolved)
        kwargs = dict(runtime_root=runtime, lane=handle.lane, candidate_id=handle.candidate_id, binding=body["binding"])
        if loader is not None:
            loader(path, **kwargs)  # v2 read-only leaf, never equivalent re-prepare.
        else:
            # v1 has no mutation branch, and the deployment check above has
            # already rejected a stale leaf before reaching its public loader.
            legacy_loader(**kwargs)
        refs.update(body["source_dependencies"])
    return refs


def authority_references(base: str) -> tuple[set[str], set[str]]:
    """Return cited paths/dirs only after every in-scope document was checked.

    Missing optional Tier A roots remain valid. A directory alias may name only
    an ordinary sibling directory that this same walk actually scans. Other
    links, unreadable inputs, oversized text, invalid UTF-8 or observed drift
    abort the scan. This is a quiet-window check, not same-user isolation.
    """
    base = os.path.abspath(base)
    out_root = os.path.join(base, "out")
    pattern = re.compile(re.escape(out_root) + r"/[^\"'\s,\]\}<>()]+")
    refs: set[str] = set()
    observed: dict[str, tuple[int, ...] | None] = {}
    aliases: dict[str, tuple[str, str]] = {}
    visited_directories: set[str] = set()
    recovery_documents: dict[str, str] = {}
    recovery_directories: set[str] = set()
    required_recovery: set[str] = set()
    recovery_pattern = re.compile(re.escape(base) + r"/\.(?:prepared-deliveries|delivery-journal|producer-batch-journal)/[^\"'\s,\]\}<>()]+\.json")

    def remember(path: str, *, directory: bool) -> os.stat_result:
        info = os.lstat(path)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(info.st_mode):
            raise AuthorityScanError(f"authority path has an unsafe type: {path}")
        identity = _scan_identity(info)
        if path in observed and observed[path] != identity:
            raise AuthorityScanError(f"authority path changed during scan: {path}")
        observed[path] = identity
        return info

    def remember_directory_entry(root: str, name: str, siblings: set[str]) -> bool:
        """Record an entry; True means a verified alias, never a skipped subtree."""
        path = os.path.join(root, name)
        info = os.lstat(path)
        if not stat.S_ISLNK(info.st_mode):
            remember(path, directory=True)
            return False
        raw_target = os.readlink(path)
        if (info.st_nlink != 1 or not raw_target
                or raw_target in {".", "..", name}
                or os.path.basename(raw_target) != raw_target
                or raw_target not in siblings):
            raise AuthorityScanError(f"authority directory alias is not a direct sibling: {path}")
        target = os.path.join(root, raw_target)
        # Reject chains/escapes. The ordinary target must remain in this same
        # walk; a stat or a familiar venv name alone is not a completeness proof.
        remember(target, directory=True)
        identity = _scan_identity(info)
        if path in observed and observed[path] != identity:
            raise AuthorityScanError(f"authority alias changed during scan: {path}")
        observed[path] = identity
        aliases[path] = (target, raw_target)
        if (_scan_identity(os.lstat(path)) != identity
                or os.readlink(path) != raw_target):
            raise AuthorityScanError(f"authority alias changed during scan: {path}")
        return True

    def walk_error(exc: OSError) -> None:
        raise AuthorityScanError(f"authority directory is unreadable: {exc.filename}") from exc

    def observe_recovery_file(path: str, *, may_be_missing: bool = False) -> None:
        try:
            remember(path, directory=False)
        except FileNotFoundError:
            if not may_be_missing:
                raise
            observed[path] = None

    remember(base, directory=True)
    for top in TIER_A + sorted(RECOVERY_STORES):
        root0 = os.path.join(base, top)
        try:
            remember(root0, directory=True)
        except FileNotFoundError:
            observed[root0] = None  # Optional absence is rechecked at the end.
            continue
        for root, directories, files in os.walk(root0, onerror=walk_error, followlinks=False):
            remember(root, directory=True)
            visited_directories.add(root)
            if top in RECOVERY_STORES:
                recovery_directories.add(root)
                if any(stat.S_ISLNK(os.lstat(os.path.join(root, name)).st_mode)
                       for name in directories):
                    raise AuthorityScanError(f"producer recovery directory link is unsafe: {root}")
            siblings = set(directories)
            # Never follow the alias. Scan its ordinary sibling once and bind
            # both names into the final namespace/identity closure instead.
            directories[:] = [name for name in directories
                              if not remember_directory_entry(root, name, siblings)]
            for name in files:
                if top in RECOVERY_STORES:
                    remember(os.path.join(root, name), directory=False)
                    if top != ".prepared-deliveries" and not name.endswith(".json"):
                        raise AuthorityScanError(f"unknown producer journal entry: {root}/{name}")
                if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                    continue
                path = os.path.join(root, name)
                info = remember(path, directory=False)
                text, companion = _authority_text(path, info)
                if companion is not None:
                    # Bind the data fork into the SAME namespace/stat closure.
                    # Its own normal iteration still performs strict UTF-8 reading.
                    if companion not in files:
                        raise AuthorityScanError(f"AppleDouble data companion is missing: {path}")
                    remember(os.path.join(root, companion), directory=False)
                required_recovery.update(recovery_pattern.findall(text))
                if top in RECOVERY_STORES:
                    recovery_documents[path] = text
                else:
                    refs.update(hit.rstrip(".,;:/") for hit in pattern.findall(text))
    try:
        recovery_refs = _prepared_recovery_references(
            base, recovery_documents, recovery_directories, required_recovery,
            observe_recovery_file,
        )
    except (RuntimeError, TypeError, KeyError, AttributeError) as exc:
        raise AuthorityScanError(f"producer recovery scan failed: {exc}") from exc
    refs.update(path for path in recovery_refs if path.startswith(out_root + "/"))
    for alias, (target, raw_target) in aliases.items():
        if target not in visited_directories:
            raise AuthorityScanError(f"authority alias target was not scanned: {alias}")
        if os.readlink(alias) != raw_target or os.lstat(alias).st_nlink != 1:
            raise AuthorityScanError(f"authority alias changed during scan: {alias}")
    # A later read must not conceal an earlier file/namespace change. The
    # caller still has to hold the existing quiet-window/runner lock.
    for path, expected in observed.items():
        try:
            actual = _scan_identity(os.lstat(path))
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise AuthorityScanError(f"authority path changed during scan: {path}")
    dirs = {r for r in refs
            if not os.path.splitext(r)[1]
            and len(os.path.relpath(r, out_root).split(os.sep)) >= 3}
    return refs, dirs


def candidate_states(base: str, *, strict: bool = False) -> tuple[dict[OwnerIdentity, str], set[OwnerIdentity]]:
    """Dated owners in this runtime, retaining conflicting claims as a hold."""
    status: dict[OwnerIdentity, str] = {}
    pending: set[OwnerIdentity] = set()
    observed: dict[str, tuple[int, ...] | None] = {}
    state_root = f"{base}/state"
    if strict:
        try:
            observed[state_root] = _scan_identity(os.lstat(state_root))
        except FileNotFoundError:
            observed[state_root] = None
    for state_file in glob.glob(f"{base}/state/2026-*.json"):
        if ".pre-" in os.path.basename(state_file):
            continue
        try:
            day = Path(state_file).stem
            _owner_identity(base, day, "state")
            if strict:
                before = os.lstat(state_file)
                observed[state_file] = _scan_identity(before)
                text, _ = _authority_text(state_file, before)
                doc = json.loads(text)
                if not isinstance(doc, dict):
                    raise ValueError("state is not an object")
            else:
                with open(state_file, encoding="utf-8") as fh:
                    doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("state is not an object")
            if doc.get("date") is not None and doc["date"] != day:
                raise AuthorityScanError(f"cleanup state date disagrees with its filename: {state_file}")
        except (OSError, ValueError) as exc:
            if strict or isinstance(exc, AuthorityScanError):
                raise AuthorityScanError(f"cleanup state is unreadable or invalid: {state_file}: {exc}") from exc
            continue
        for key in ("picks", "songs", "talk_superseded_attempts",
                    "song_superseded_attempts", "operator_superseded_picks",
                    "operator_quarantined_picks"):
            if doc.get(key) is not None and (
                not isinstance(doc[key], list)
                or any(not isinstance(row, dict) for row in doc[key])
            ):
                raise AuthorityScanError(f"cleanup state rows are invalid: {state_file}: {key}")
            for row in doc.get(key) or []:
                if isinstance(row, dict) and row.get("candidate_id"):
                    if (not isinstance(row["candidate_id"], str)
                                   or (row.get("status") is not None and not isinstance(row["status"], str))):
                        raise AuthorityScanError(f"cleanup owner claim is invalid: {state_file}: {key}")
                    if row.get("date") is not None and row["date"] != day:
                        raise AuthorityScanError(f"cleanup owner date disagrees with its state: {state_file}: {key}")
                    try:
                        identity = _owner_identity(base, day, row["candidate_id"])
                    except ValueError as exc:
                        raise AuthorityScanError(f"cleanup owner identity is invalid: {state_file}: {key}") from exc
                    claim = row.get("status") or "unknown"
                    previous = status.get(identity, claim)
                    status[identity] = claim if previous == claim else "cleanup_owner_conflict"
        for key in ("pending_talk", "pending_song"):
            if doc.get(key) is not None and (
                not isinstance(doc[key], list)
                or any(not isinstance(row, dict) for row in doc[key])
            ):
                raise AuthorityScanError(f"cleanup pending rows are invalid: {state_file}: {key}")
            for row in doc.get(key) or []:
                if isinstance(row, dict):
                    if row.get("date") is not None and row["date"] != day:
                        raise AuthorityScanError(f"cleanup pending date disagrees with its state: {state_file}: {key}")
                    if not any(isinstance(row.get(field), str) and row[field]
                                          for field in ("cid", "candidate_id")):
                        raise AuthorityScanError(f"cleanup pending owner is missing: {state_file}: {key}")
                    if row.get("cid") and row.get("candidate_id") and row["cid"] != row["candidate_id"]:
                        raise AuthorityScanError(f"cleanup pending owner claims conflict: {state_file}: {key}")
                    for field in ("cid", "candidate_id"):
                        if row.get(field):
                            if not isinstance(row[field], str):
                                raise AuthorityScanError(f"cleanup pending claim is invalid: {state_file}: {key}")
                            try:
                                pending.add(_owner_identity(base, day, row[field]))
                            except ValueError as exc:
                                raise AuthorityScanError(f"cleanup pending identity is invalid: {state_file}: {key}") from exc
    for path, expected in observed.items():
        try:
            actual = _scan_identity(os.lstat(path))
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise AuthorityScanError(f"cleanup state changed during qualification: {path}")
    return status, pending


def in_flight_candidates(base: str) -> set[OwnerIdentity]:
    """Candidates someone is mid-replacement on, which state cannot tell us.

    A terminal status is not the same as "nobody is working on it": the 1323
    three-way replacement was the only in-flight delivery while
    its state row read `published`. Two on-disk signals mark real in-flight
    work — a staging directory under recovery/, and a
    `replacement_recuts.pre-*` freeze snapshot, which exists precisely to roll
    a replacement back.
    """
    found: set[OwnerIdentity] = set()
    for staging in glob.glob(f"{base}/recovery/*/*"):
        if not os.path.isdir(staging) and not CANDIDATE_ID.fullmatch(os.path.basename(staging)):
            continue
        day, cid = Path(staging).relative_to(Path(base) / "recovery").parts
        if not CANDIDATE_ID.fullmatch(cid):
            raise AuthorityScanError(f"cleanup replacement owner identity is invalid: {staging}")
        try:
            found.add(_owner_identity(base, day, cid))
        except ValueError as exc:
            raise AuthorityScanError(f"cleanup replacement owner identity is invalid: {staging}") from exc
    for frozen in glob.glob(f"{base}/out/*/*/replacement_recuts.pre-*"):
        try:
            found.add(owning_identity(base, frozen))
        except ValueError as exc:
            raise AuthorityScanError(f"cleanup replacement owner identity is invalid: {frozen}") from exc
    return found


def eligibility_hold_reason(base, path, refs, refdirs, status, pending, in_flight) -> str | None:
    """The same current deletion-qualification gates for planning and applying."""
    if path in refs or any(path.startswith(d + "/") for d in refdirs):
        return "gate2_authority_cited"
    try:
        identity = owning_identity(base, path)
    except ValueError:
        return "gate3_owner_identity_unknown"
    if identity in pending:
        return "gate3_owner_has_queued_work"
    if identity in in_flight:
        return "gate3_owner_mid_replacement"
    if status.get(identity) == "cleanup_owner_conflict":
        return "gate3_owner_conflicting_state"
    if status.get(identity, "unknown") not in TERMINAL_STATES:
        return "gate3_owner_not_terminal"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="/opt/bilive/autoslice")
    parser.add_argument("--json", help="write the gated plan here")
    parser.add_argument("--allow-source-extractions", action="store_true",
                        help="also plan _source.mp4/_full_source.mp4 (new class — disclose it)")
    parser.add_argument("--ignore-quiet-window", action="store_true",
                        help="plan anyway while production is live; NEVER pass this before deleting")
    args = parser.parse_args()

    base = os.path.abspath(args.base)
    out_root = f"{base}/out"

    problems = quiet_window(base)
    print("GATE 0 quiet window:", "CLEAR" if not problems else "BLOCKED")
    for problem in problems:
        print(f"   ! {problem}")
    if problems and not args.ignore_quiet_window:
        print("\nRefusing to plan against a live host. Re-run in a quiet window, "
              "or pass --ignore-quiet-window to size the surface only (never to delete).")
        return 2

    allowed = set(ALLOWED_CLASSES)
    if args.allow_source_extractions:
        allowed |= SOURCE_EXTRACTION_CLASSES

    try:
        refs, refdirs = authority_references(base)
    except (OSError, ValueError) as exc:
        print(f"GATE 2 authority: BLOCKED — incomplete reference scan: {exc}")
        print("No cleanup plan emitted; existing plan files were not refreshed.")
        return 2
    try:
        status, pending = candidate_states(base)
        in_flight = in_flight_candidates(base)
    except (OSError, ValueError) as exc:
        print(f"GATE 3 owner identity: BLOCKED — {exc}")
        print("No cleanup plan emitted; existing plan files were not refreshed.")
        return 2
    print(f"GATE 2 authority: {len(refs)} out/ paths cited, {len(refdirs)} specific dirs")
    print(f"GATE 3 states: {len(status)} candidates known, {len(pending)} with queued work, "
          f"{len(in_flight)} mid-replacement on disk")

    plan, held = [], collections.Counter()
    for root, _, files in os.walk(out_root):
        for name in files:
            if os.path.splitext(name)[1].lower() not in MEDIA_EXT:
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            day = os.path.relpath(path, out_root).split(os.sep)[0]
            klass = classify(name)
            if klass not in allowed:
                held["gate1_class_not_allowlisted"] += size
                continue
            reason = eligibility_hold_reason(base, path, refs, refdirs, status, pending, in_flight)
            if reason:
                held[reason] += size
                continue
            identity = owning_identity(base, path)
            try:
                preimage = capture_preimage(base, path)
                if preimage["bytes"] != size:
                    raise ValueError("file size changed during planning")
            except (OSError, ValueError):
                held["gate1_unsafe_or_changed_file"] += size
                continue
            plan.append({"path": path, "bytes": size, "day": day, "cls": klass,
                         "owner": identity[2], "owner_status": status.get(identity),
                         "owner_identity": owner_claim(identity),
                         "preimage": preimage})

    print("\nHELD BACK:")
    for reason, size in held.most_common():
        print(f"   {size / 2**30:8.2f} GiB  {reason}")
    print(f"\nPLANNED FOR DELETION: {len(plan)} files, "
          f"{sum(p['bytes'] for p in plan) / 2**30:.2f} GiB")
    by_day = collections.Counter()
    for item in plan:
        by_day[item["day"]] += item["bytes"]
    for day, size in sorted(by_day.items()):
        print(f"   {day:13} {size / 2**30:8.2f} GiB")
        source = (f"/path/to/cloud-drive/live-streaming/"
                  f"22966160/{day}")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            print(f"       CloudFS source dir present: {os.path.isdir(source)} "
                  "(confirm the mount is healthy before trusting a False)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.json}")
    print("\nThis tool never deletes. Feed the plan to a reviewed delete step, "
          "and re-run scan_state_dangling_media_refs.py afterwards: the count of "
          "dangling references must not increase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
