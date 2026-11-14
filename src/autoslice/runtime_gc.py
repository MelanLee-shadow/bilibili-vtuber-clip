"""Lifecycle collection of regenerable runtime media; never collect recordings.

Plans use file metadata, live /proc activity and structured lifecycle records,
including the publication registry.  They deliberately do not scan prose
references or hash media. The default is read-only; applying an explicitly
reviewed plan does not enable future applies.
"""

from __future__ import annotations

import argparse
import ctypes
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import time

from src.autoslice.batch_items import ItemBatch
from src.autoslice import evidence_lease

DAY = 86400
GIB = 1024**3
SCHEMA = "runtime-lifecycle-gc.v1"
SELF_TOUCH_SCHEMA = "runtime-gc-self-touch.v2"
MEDIA = frozenset({
    ".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".m4s", ".avi",
    ".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".pcm",
    ".f32le", ".s16le", ".raw", ".png", ".jpg", ".jpeg", ".webp", ".gif",
})
MEDIA_ROLES = frozenset({"video", "audio", "cover", "cover_title_mask", "cover_pre_overlay", "cover_route_background"})
ACTIVE = frozenset({"running", "processing", "in_progress", "producing", "rendering"})
RECORDING = re.compile(r"\d+_\d{8}-\d{2}-\d{2}-\d{2}\.(mp4|flv|mkv|ts|m4s)$", re.I)
SKIP_DIRS = frozenset({"recording", "recordings", "source-recordings"})
NO_MEDIA_DIRS = frozenset({".git", ".venv", "venv", "node_modules"})
VERIFIED = frozenset({"VERIFIED_PUBLIC", "VERIFIED_SAME_BV", "VERIFIED_SAME_BV_COVER"})
RUNTIME_REGISTRY = Path("state") / "publication_registry.runtime.v1.json"
TERMINAL_DISCARD = frozenset({"candidate_rejected", "rejected", "abandoned", "superseded"})
# A concluded operator run declares itself terminal; free-form RESULT statuses do not.
RUN_LIFECYCLE, RUN_LIFECYCLE_SCHEMA = "RUN-LIFECYCLE.json", "run-lifecycle.v1"
# Non-media reclaim of terminal runs (docs/pipeline/10 "运行时媒体生命周期 GC").
NONMEDIA_CATEGORIES = frozenset({"out", "private-runs"})
DECISION_RECORD_MAX_BYTES = 1024**2
DECISION_KIND = re.compile(
    r"(^|[._-])(spec|recipe|range|interval|boundary|clock|result|state|status|done|decision|verdict|"
    r"receipt|record|checkpoint|provenance|clip-context|manifest|upload|uploaded|public_verify|"
    r"season_verify|publication|registry|completed|usage|journal|ledger|readme)([._-]|$)", re.I)
CODE_SUFFIXES = frozenset({".py", ".pyc", ".pyo"})
CODE_DIRS = frozenset({"__pycache__", ".pytest_cache", "site-packages", ".venv", "venv", "node_modules"})
COMMIT_FILES = frozenset({"DEPLOYED_COMMIT", "SOURCE_COMMIT"})
COMMIT_FIELDS = ("deployed_commit", "source_commit", "code_commit", "readlink_current")
CODE_SOURCE_SCHEMA = "runtime-gc-code-source.v1"


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def metadata_json(path: Path) -> dict:
    """Historic run summaries are optional; malformed prose cannot veto all GC."""
    try:
        if path.exists() and path.stat().st_size > 16 * 1024**2:
            return {}
        return read_json(path)
    except (ValueError, UnicodeError):
        return {}


def identity(info: os.stat_result) -> dict:
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "links": info.st_nlink}


def under(path: Path, parent: Path) -> bool:
    # Path.parents constructs every intermediate Path on each query. This
    # predicate is hot for every media × live-path/publication/group lookup.
    return (not path.is_absolute() if not parent.parts else
            path.parts[:len(parent.parts)] == parent.parts)


def _published(row: dict) -> bool:
    """Use the existing verified publication projection, without media hashing."""
    proof = row.get("publication_reconciliation") or {}
    coverage = (proof.get("content_coverage") or {}) if isinstance(proof, dict) else {}
    if row.get("_registry"):
        # A registry row is already the loader-validated publication authority.
        return (row.get("status") == "published" and isinstance(proof, dict)
                and proof.get("schema_version") == "publication-reconciliation.v1"
                and proof.get("status") in VERIFIED and isinstance(proof.get("authority"), Mapping))
    return bool(
        isinstance(proof, dict)
        and proof.get("schema_version") == "publication-reconciliation.v1"
        and row.get("status") in {"published", "covered_by_publication"}
        and proof.get("status") in {"VERIFIED_PUBLIC", "VERIFIED_SAME_BV", "VERIFIED_SAME_BV_COVER"}
        and proof.get("bvid") == row.get("bvid") and str(row.get("bvid", "")).startswith("BV")
        and (proof.get("candidate_id") == row.get("candidate_id")
             or isinstance(coverage, dict) and coverage.get("schema_version") == "publication-content-coverage.v1"
             and row.get("candidate_id") in coverage.get("covered_candidate_ids", []))
    )


def _authority_path(row: dict) -> Path:
    proof = row.get("publication_reconciliation")
    authority = proof.get("authority") if isinstance(proof, dict) else None
    return Path(str(authority.get("path") or "/")) if isinstance(authority, Mapping) else Path("/")


def state_rows(base: Path) -> dict[tuple[str, str], dict]:
    rows = {}
    for path in sorted((base / "state").glob("????-??-??.json")):
        state = read_json(path)
        # Current rows outrank historical attempts with the same candidate id.
        for key in ("talk_superseded_attempts", "song_superseded_attempts", "picks", "songs", "pending_talk", "pending_song"):
            for raw in state.get(key, []) if isinstance(state.get(key), list) else []:
                if not isinstance(raw, dict):
                    continue
                cid = str(raw.get("candidate_id") or raw.get("cid") or "")
                if not cid or "/" in cid or cid in {".", ".."}:
                    continue
                row = dict(raw)
                if "superseded" in key:
                    row["status"] = "superseded"
                elif key.startswith("pending"):
                    row["status"] = row.get("status") or "blocked"
                rows[(path.stem, cid)] = row
    return rows


@dataclass
class Publications:
    """Bytes the publication registry still needs; no lifecycle collects them."""
    base: Path
    rows: dict[tuple[str, str], dict] = field(default_factory=dict)
    inputs: set[Path] = field(default_factory=set)
    unresolved_dirs: set[Path] = field(default_factory=set)
    unresolved: re.Pattern | None = None
    held: re.Pattern | None = None

    def guard(self, path: Path) -> str:
        if path in self.inputs:
            return "published-input"
        name = str(path.relative_to(self.base)) if under(path, self.base) else str(path)
        if self.unresolved and self.unresolved.search(name) or any(under(path, d) for d in self.unresolved_dirs):
            return "published-unresolved"
        return "registry-hold" if self.held and self.held.search(name) else ""

    def guards_group(self, cid: str) -> str:
        if cid and self.unresolved and self.unresolved.fullmatch(cid):
            return "published-unresolved"
        return "registry-hold" if cid and self.held and self.held.fullmatch(cid) else ""


def _id_pattern(ids: set[str]) -> re.Pattern | None:
    ids = {cid for cid in ids if cid}
    return re.compile("|".join(re.escape(cid) for cid in sorted(ids, key=len, reverse=True))) if ids else None


def publications(base: Path, rows: dict, registry_path: Path | None = None) -> Publications:
    """Registry plus daily rows; an unreadable registry raises (fail closed)."""
    from src.autoslice.first_publication_boundary import published_recall_inputs
    from src.autoslice.publication_registry import _candidate_ids, load_publication_registry

    registry = load_publication_registry(registry_path, runtime_path=base / RUNTIME_REGISTRY)
    result, published, held, unresolved = Publications(base), [], set(), set()
    for row in registry["entries"]:
        if row.get("status") == "published":
            ids = sorted(_candidate_ids(row) - {""})
            for cid in ids:
                result.rows[(str(row.get("recording_date") or ""), cid)] = {**row, "candidate_id": cid, "_registry": True}
            published.append((row, ids))
        elif row.get("status") == "hold_pending_review":
            held.add(str(row["candidate_id"]))
    published.extend((row, [cid]) for (_, cid), row in rows.items()
                     if row.get("status") in {"published", "covered_by_publication"})
    for row, ids in published:
        proof = row.get("publication_reconciliation")
        authority = proof.get("authority") if isinstance(proof, Mapping) else None
        if not isinstance(authority, Mapping) or not authority.get("path"):
            unresolved.update(ids)
            continue
        path = Path(str(authority["path"]))
        files, complete = published_recall_inputs(path, ids, within=base)
        result.inputs |= files
        if not complete:
            unresolved.update(ids)
            if base in path.parent.parents:  # the package directory, never the whole runtime
                result.unresolved_dirs.add(path.parent)
    result.unresolved, result.held = _id_pattern(unresolved), _id_pattern(held)
    return result


@dataclass
class Activity:
    paths: set[Path] = field(default_factory=set)
    inodes: set[tuple[int, int]] = field(default_factory=set)
    candidate_ids: set[str] = field(default_factory=set)
    runner_locked: bool = False


def activity(base: Path, proc: Path = Path("/proc")) -> Activity:
    """Read real open descriptors and kernel locks, including cwd/spec ownership."""
    result = Activity()
    if not proc.is_dir():
        raise RuntimeError("live /proc inspection is required")
    locked = set()
    for line in (proc / "locks").read_text().splitlines():
        for token in line.split():
            if re.fullmatch(r"[0-9a-fA-F]+:[0-9a-fA-F]+:\d+", token):
                major, minor, inode = token.split(":")
                locked.add((os.makedev(int(major, 16), int(minor, 16)), int(inode)))
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            descriptors = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise RuntimeError(f"cannot inspect process {process.name}") from exc
        try:
            result.paths.add(Path(os.readlink(process / "cwd").removesuffix(" (deleted)")))
        except FileNotFoundError:
            pass
        for descriptor in descriptors:
            try:
                target = Path(os.readlink(descriptor).removesuffix(" (deleted)"))
                info = descriptor.stat()
            except FileNotFoundError:
                continue
            result.paths.add(target)
            inode = (info.st_dev, info.st_ino)
            result.inodes.add(inode)
            if inode in locked and target == base / "runner.lock":
                result.runner_locked = True
        for arg in args:
            if arg.startswith("/"):
                result.paths.add(Path(arg))
        if "--spec" in args:
            position = args.index("--spec") + 1
            if position < len(args):
                spec = read_json(Path(args[position]))
                cid = str(spec.get("candidate_id") or spec.get("cid") or "")
                if cid:
                    result.candidate_ids.add(cid)
    return result


def self_touch_path(base: Path) -> Path:
    return base / "state" / "runtime-gc-self-touch.json"


def directory_stamp(info: os.stat_result) -> dict[str, int]:
    return {"mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "device": info.st_dev, "inode": info.st_ino}


class UnlinkEvents:
    """Observe only this unlink's directory events; no persistent watcher."""

    def __init__(self, path: Path):
        self.fd = None
        self.name = os.fsencode(path.name)
        if sys.platform != "linux":
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            self.fd = fd
            self.wd = libc.inotify_add_watch(fd, os.fsencode(path.parent), 0xFCE)
            if self.wd < 0:
                self.close()
        except (AttributeError, OSError):
            self.close()

    def only_our_delete(self) -> bool | None:
        if self.fd is None:
            return None  # No causal evidence: do not exclude a directory time.
        seen = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return seen
            if not data:
                return False
            offset = 0
            while offset < len(data):
                wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                name = data[offset + 16:offset + 16 + length].split(b"\0", 1)[0]
                offset += 16 + length
                # Overflow, an external edit (even with restored mtime), or
                # a temporary create/delete cannot be attributed to our rm.
                if wd != self.wd or mask != 0x200 or name != self.name:
                    return False
                seen = True

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def read_self_touch(base: Path) -> dict[str, dict[str, int]]:
    """Directory mtimes GC itself caused by unlinking; unreadable means no exclusions."""
    try:
        document = read_json(self_touch_path(base))
        dirs = document.get("dirs")
        if document.get("schema_version") != SELF_TOUCH_SCHEMA or not isinstance(dirs, dict):
            return {}
        return {k: v for k, v in dirs.items() if isinstance(k, str) and isinstance(v, dict)
                and set(v) == {"mtime_ns", "ctime_ns", "device", "inode"}
                and all(type(n) is int for n in v.values())}
    except Exception:
        return {}


def write_self_touch(base: Path, touched: dict[str, dict[str, int]]) -> None:
    """Merge, prune stale entries and atomically persist; never raises."""
    try:
        merged = {**read_self_touch(base), **touched}
        kept = {}
        for name, stamp in merged.items():
            try:
                if directory_stamp(os.stat(name)) == stamp:
                    kept[name] = stamp
            except OSError:
                continue
        target = self_touch_path(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps({"schema_version": SELF_TOUCH_SCHEMA, "dirs": kept}, sort_keys=True) + "\n",
                            encoding="utf-8")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    except Exception:
        pass


def activity_mtime(path: Path, info: os.stat_result, touched: dict[str, dict[str, int]]) -> float:
    """Entry mtime, except a directory whose mtime is exactly GC's own unlink."""
    if stat.S_ISDIR(info.st_mode) and touched.get(str(path)) == directory_stamp(info):
        return 0
    return info.st_mtime


@dataclass
class Group:
    root: Path
    category: str
    cid: str = ""
    loose: bool = False
    media: list[tuple[Path, os.stat_result, bool]] = field(default_factory=list)
    nonmedia: list[tuple[Path, os.stat_result, str]] = field(default_factory=list)
    status: str = "idle"
    published: bool = False
    current_state: bool = False
    published_at: float = 0
    last_write: float = 0
    run_scoped: bool = False
    declared: bool = False
    verified: bool = False
    commit_hints: set[str] = field(default_factory=set)

    def owns(self, path: Path) -> bool:
        if self.loose:
            return path.parent == self.root and (path.name == self.cid or path.name.startswith(self.cid + ".")
                                                or f"__{self.cid}." in path.name)
        return under(path, self.root)

    def lifecycle_applies(self, path: Path) -> bool:
        """A candidate's lifecycle covers its own subtree unless the run is the candidate's."""
        if self.run_scoped or self.loose:
            return True
        return bool(self.cid) and any(self.cid in part for part in path.relative_to(self.root).parts)


def nonmedia_kind(relative: Path, info: os.stat_result, in_repo_copy: bool) -> str:
    """Reclaimable class of a terminal run's non-media file; "" keeps it.

    Code: copies of the repository tree (a directory holding ``src/autoslice``)
    and interpreter/test caches, rebuildable from git.  Cache: JSON/JSONL above
    the decision-record cap whose name is not a decision kind.  Everything else,
    including run-local scripts and all text records, stays.
    """
    if CODE_DIRS.intersection(relative.parts) or in_repo_copy and relative.suffix.lower() in CODE_SUFFIXES:
        return "code"
    if (relative.suffix.lower() in {".json", ".jsonl"} and info.st_size > DECISION_RECORD_MAX_BYTES
            and not DECISION_KIND.search(relative.name)):
        return "cache"
    return ""


def commit_hints(metadata: dict, path: Path | None = None) -> set[str]:
    values = [str(metadata.get(key) or "") for key in COMMIT_FIELDS]
    if path is not None:
        values.append(path.read_text(encoding="utf-8", errors="replace"))
    return {match for value in values for match in re.findall(r"\b[0-9a-f]{40}\b", value)}


def roots(base: Path, home: Path, issues: ItemBatch | None = None) -> list[tuple[Path, str]]:
    issues = issues if issues is not None else ItemBatch()
    found = [(base / name, name) for name in ("out", "private-runs", "reports", "candidates", ".prepared-deliveries")]
    # Existing delivery and the stable root used by versioned deployments.
    found.extend((p, "delivery") for p in (base / "lidousha", base / "repo" / "lidousha"))
    found.extend((p, "old-deployment") for p in base.iterdir()
                 if p.name.startswith((".oci3-shadow-upload-", "repo.rollback-", "repo.stage-")))
    with issues.isolate(str(home)):
        if home.is_dir():
            found.extend((p, "old-clone") for p in home.glob("vtuber*"))
    result = []
    for path, category in found:
        with issues.isolate(str(path)):
            # Configured roots that never existed are normal, not failures.
            if path.is_dir() and not path.is_symlink():
                result.append((path, category))
    return result


def walk_entries(root: Path):
    info = root.stat()
    device = info.st_dev
    pending = [root]
    while pending:
        directory = pending.pop()
        # Directory activity must be sampled when entered, not cached when
        # its parent was listed: a create/delete in between is real activity.
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
            raise ValueError(f"ITEM_CHANGED_DURING_WALK: {directory}")
        yield directory, info
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                path = directory / entry.name
                if info.st_dev != device:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if entry.name not in SKIP_DIRS:
                        pending.append(path)
                else:
                    yield path, info


def groups(base: Path, home: Path, rows: dict, candidate_ids: set[str] | None = None,
           issues: ItemBatch | None = None) -> list[Group]:
    result = []
    issues = issues if issues is not None else ItemBatch()
    touched = read_self_touch(base)
    ids = sorted({cid for _, cid in rows}, key=len, reverse=True)
    owned_paths = {Path(value) for (_, cid), row in rows.items() if candidate_ids and cid in candidate_ids
                   for value in row.values() if isinstance(value, str) and value.startswith("/")}

    def current_row(group, metadata):
        date = str(metadata.get("date") or metadata.get("recording_date") or "")
        if not date:
            date = next((p for p in group.root.parts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p)), "")
        matches = [row for (day, cid), row in rows.items() if cid == group.cid and (not date or date == day)]
        if len(matches) == 1:
            return matches[0], True
        # An ambiguous historical run is subject to idle retention. A current
        # active candidate still wins even if its run omitted a date.
        active = next((row for row in matches if row.get("status") in ACTIVE), None)
        return (active, True) if active else (metadata, False)
    def children(path):
        found = []
        with issues.isolate(str(path)):
            found = list(path.iterdir())
        return found

    def containers_for(root, category):
        if category in {"old-clone", "old-deployment"}:
            return [Group(root, category)]
        if category == ".prepared-deliveries":
            def directories(path):
                found = []
                for child in children(path):
                    with issues.isolate(str(child)):
                        if stat.S_ISDIR(child.lstat().st_mode):
                            found.append(child)
                return found
            return [Group(generation, category, cid.name)
                    for lane in directories(root) for cid in directories(lane)
                    for generation in directories(cid)]
        result = []
        parents = children(root) if category in {"out", "delivery"} else [root]
        for parent in parents:
            with issues.isolate(str(parent)):
                info = parent.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    continue
                loose, incomplete = {}, set()
                for child in children(parent):
                    cid = next((cid for cid in ids if child.name.startswith(cid + ".") or f"__{cid}." in child.name
                                or child.name == f"spec_{cid}.json"), child.name)
                    if category not in {"out", "delivery"}:
                        cid = child.name
                    before = len(issues.failures)
                    with issues.isolate(str(child)):
                        child_info = child.lstat()
                        if stat.S_ISDIR(child_info.st_mode):
                            result.append(Group(child, category, child.name if category in {"out", "delivery"} else ""))
                        elif stat.S_ISREG(child_info.st_mode):
                            group = loose.setdefault(cid, Group(parent, category, cid, True))
                            group.last_write = max(group.last_write, activity_mtime(parent, info, touched), child_info.st_mtime)
                            if child.suffix.lower() in MEDIA:
                                group.media.append((child, child_info, category == "delivery" and child.suffix.lower() == ".mp4"))
                    if len(issues.failures) != before:
                        incomplete.add(cid)
                result.extend(group for cid, group in loose.items() if cid not in incomplete)
        return result

    for root, category in roots(base, home, issues):
        containers = containers_for(root, category)
        for group in containers:
            with issues.isolate(str(group.root)):
                if candidate_ids and group.cid not in candidate_ids and not any(cid in group.root.name for cid in candidate_ids):
                    if not any(group.owns(path) for path in owned_paths):
                        spec = metadata_json(group.root / "SPEC.json") or metadata_json(group.root / "spec.json")
                        if str(spec.get("candidate_id") or spec.get("cid") or "") not in candidate_ids:
                            continue
                metadata = {}
                for name in ("STATE.json", "state.json", "RESULT.json", "result.json", "SPEC.json", "spec.json", "prepared.json"):
                    document = metadata_json(group.root / name) if not group.loose else {}
                    metadata.update(document)
                declared = metadata_json(group.root / RUN_LIFECYCLE) if not group.loose else {}
                run_status = str(declared.get("status") or "").lower() if declared.get("schema_version") == RUN_LIFECYCLE_SCHEMA else ""
                run_status = run_status if run_status in TERMINAL_DISCARD else ""
                own_cid = str(metadata.get("candidate_id") or metadata.get("cid") or "")
                group.cid = group.cid or own_cid
                group.run_scoped = (category in {"out", "delivery", ".prepared-deliveries"} or bool(run_status)
                                    or bool(group.cid) and group.cid == own_cid)
                group.commit_hints = commit_hints(metadata)

                def settle(row):
                    group.status = str(row.get("status") or "idle").lower()
                    if run_status and not group.current_state:
                        group.status = run_status  # the run's own terminal record outranks free-form notes
                    group.declared = bool(run_status)
                    # A publication concludes the run that published it (the one holding its
                    # authority), not later repair/recall runs on the same candidate.
                    group.verified = _published(row)
                    group.published = group.verified and under(_authority_path(row), group.root)
                row, group.current_state = current_row(group, metadata)
                settle(row)
                typed_media = set()
                finals = set()
                if not group.loose:
                    files = list(walk_entries(group.root))
                    group.last_write = max(activity_mtime(path, info, touched) for path, info in files)
                    if category in NONMEDIA_CATEGORIES:
                        copies = {path.parent.parent for path, info in files if stat.S_ISDIR(info.st_mode)
                                  and path.name == "autoslice" and path.parent.name == "src"}
                        for path, info in files:
                            if not stat.S_ISREG(info.st_mode) or path.suffix.lower() in MEDIA:
                                continue
                            if path.name in COMMIT_FILES and info.st_size <= 128:
                                group.commit_hints |= commit_hints({}, path)
                            relative = path.relative_to(group.root)
                            kind = nonmedia_kind(relative, info, not copies.isdisjoint(path.parents))
                            if kind:
                                group.nonmedia.append((path, info, kind))
                    for path, _info in files:
                        if NO_MEDIA_DIRS.intersection(path.relative_to(group.root).parts):
                            continue
                        if path.name == "prepared.json":
                            for artifact in metadata_json(path).get("artifacts", []):
                                if not isinstance(artifact, dict):
                                    continue
                                staged = Path(str(artifact.get("staged_path") or ""))
                                source = Path(str(artifact.get("source_path") or ""))
                                if under(staged, path.parent) and (artifact.get("role") in MEDIA_ROLES or source.suffix.lower() in MEDIA):
                                    typed_media.add(staged)
                                    if artifact.get("role") == "video":
                                        finals.add(staged)
                        elif path.name.endswith("upload_manifest.json"):
                            video = metadata_json(path).get("video", {})
                            if isinstance(video, dict) and video.get("path"):
                                finals.add(Path(str(video["path"])))
                    for path, info in files:
                        if (not NO_MEDIA_DIRS.intersection(path.relative_to(group.root).parts)
                                and stat.S_ISREG(info.st_mode) and (path.suffix.lower() in MEDIA or path in typed_media)):
                            group.media.append((path, info, path in finals or path.name.endswith(".recut.burned-final-sapphire72.mp4")))
                for key in ("video_path", "mp4_path", "delivery_path"):
                    if isinstance(row.get(key), str):
                        finals.add(Path(row[key]))
                group.media = [(p, info, final or p in finals) for p, info, final in group.media]
                if not group.cid:
                    matched = {cid for p, _, _ in group.media for cid in ids if cid in p.name}
                    if len(matched) == 1:
                        group.cid = matched.pop()
                        row, group.current_state = current_row(group, metadata)
                        settle(row)
                if group.verified:
                    stamp = (row.get("publication_reconciliation") or {}).get("reconciled_at")
                    try:
                        group.published_at = datetime.fromisoformat(str(stamp)).timestamp()
                    except ValueError:
                        pass
                if group.media or group.nonmedia:
                    result.append(group)
    return result


def _retention(group: Group, final: bool, pressure: bool, applies: bool = True) -> tuple[str, int]:
    if not applies:  # another candidate's files in a shared run keep the idle clock
        return "idle", (2 if pressure else 7) * DAY
    if group.status in TERMINAL_DISCARD:
        return "terminal-discard", 0
    if group.published or group.verified and final:
        # A verified candidate's final keeps the verification clock in any run;
        # only the publishing run gives up its other intermediates at once.
        return ("published-final", DAY if pressure else 7 * DAY) if final else ("published-intermediate", 0)
    if group.status in {"published", "covered_by_publication"} and not group.verified:
        return "publication-unverified", 7 * DAY
    return ("blocked" if group.status in {"blocked", "failed", "speaker_evidence_insufficient"} else "idle"), (2 if pressure else 7) * DAY


def collect(base: Path, *, home: Path = Path("/home/user"), now: float | None = None,
            free_bytes: int | None = None, live: Activity | None = None,
            preserve: set[str] | None = None, candidate_ids: set[str] | None = None,
            registry: Path | None = None, lease_base: Path | None = None) -> dict:
    base = base.absolute()
    now = time.time() if now is None else now
    free_bytes = shutil.disk_usage(base).free if free_bytes is None else free_bytes
    live = activity(base) if live is None else live
    pressure = free_bytes < 20 * GIB
    keep = set(preserve or ())
    leases = evidence_lease.read_leases(lease_base or base, now=now)
    lease_index = evidence_lease.LeaseIndex(leases)
    rows = state_rows(base)
    # The registry is the publication authority; daily state may not exist.
    pubs = publications(base, rows, registry)
    rows = {**rows, **pubs.rows}
    deletes, retained, retained_nonmedia, summary, by_lifecycle = [], Counter(), Counter(), {}, {}
    code_source = Counter()
    issues = ItemBatch()
    self_times = read_self_touch(base)

    def add(group, path, info, reason, kind):
        current = path.lstat()
        if identity(current) != identity(info):
            raise ValueError(f"ITEM_CHANGED_DURING_PLAN: {path}")
        parent_info = path.parent.stat()
        if activity_mtime(path.parent, parent_info, self_times) > now - DAY:
            (retained if kind == "media" else retained_nonmedia)["recent-write"] += info.st_size
            return
        entry = {"path": str(path), "category": group.category, "lifecycle": reason, "kind": kind,
                 "owner": str(group.root), "candidate_id": group.cid, "identity": identity(info),
                 "parent_identity": directory_stamp(parent_info),
                 "allocated_bytes": info.st_blocks * 512 if info.st_nlink == 1 else 0}
        if kind == "code":
            entry["commit_hints"] = sorted(group.commit_hints)
            code_source["recorded_commit" if group.commit_hints else "blob_manifest_only"] += entry["allocated_bytes"]
        deletes.append(entry)
        for bucket in (summary.setdefault(group.category, {"files": 0, "logical_bytes": 0, "allocated_bytes": 0}),
                       by_lifecycle.setdefault(reason, {"files": 0, "logical_bytes": 0, "allocated_bytes": 0})):
            bucket["files"] += 1
            bucket["logical_bytes"] += info.st_size
            bucket["allocated_bytes"] += entry["allocated_bytes"]

    for group in groups(base, home, rows, candidate_ids, issues):
        mark = len(deletes)
        counters = [retained, retained_nonmedia, code_source]
        snapshots = [counter.copy() for counter in counters]
        summary_before = {key: dict(value) for key, value in summary.items()}
        lifecycle_before = {key: dict(value) for key, value in by_lifecycle.items()}
        failures_before = len(issues.failures)
        with issues.isolate(str(group.root)):
            if candidate_ids and group.cid not in candidate_ids and not any(any(cid in p.name for cid in candidate_ids) for p, _, _ in group.media):
                continue
            busy = (group.cid in live.candidate_ids or any(group.owns(p) for p in live.paths)
                    or group.category == "out" and live.runner_locked)
            last_media_write = max((info.st_mtime for _, info, _ in group.media), default=0)
            recent = max(last_media_write, group.last_write) > now - DAY
            group_guard = pubs.guards_group(group.cid)
            for path, info, final in group.media:
                reason, seconds = _retention(group, final, pressure, group.lifecycle_applies(path))
                protection = ("recording" if RECORDING.fullmatch(path.name) else
                              "explicit-preserve" if str(path) in keep else
                              "evidence-lease" if lease_index.covering(path) else
                              group_guard or pubs.guard(path))
                protection = protection or (
                    "process-or-lock" if busy or (info.st_dev, info.st_ino) in live.inodes else
                    "recent-write" if recent else
                    "in-progress" if group.current_state and group.status in ACTIVE else
                    "publication-unverified" if reason == "publication-unverified" else
                    "retention" if now - (max(info.st_mtime, group.published_at) if reason == "published-final" else last_media_write) < seconds else ""
                )
                if protection:
                    retained[protection] += info.st_size
                else:
                    add(group, path, info, reason, "media")
            # Terminal runs also give up bulk caches and repository copies; decision
            # records, subtitles and run-local scripts always stay.
            # Operator runs give up non-media only by their own record or by being the
            # publishing run; a daily verdict concludes the runner's own out/ directory.
            terminal = (group.published or group.category == "out" and group.status in TERMINAL_DISCARD
                        or group.declared and not (group.current_state and group.status in ACTIVE))
            for path, info, kind in group.nonmedia if terminal else ():
                if not group.lifecycle_applies(path):
                    continue
                protection = ("explicit-preserve" if str(path) in keep else
                              "evidence-lease" if lease_index.covering(path) else group_guard or pubs.guard(path)) or (
                    "process-or-lock" if busy or (info.st_dev, info.st_ino) in live.inodes else
                    "recent-write" if recent else
                    "in-progress" if group.current_state and group.status in ACTIVE else "")
                if protection:
                    retained_nonmedia[protection] += info.st_size
                else:
                    add(group, path, info, "terminal-" + kind, kind)
        if len(issues.failures) != failures_before:
            del deletes[mark:]
            for counter, snapshot in zip(counters, snapshots):
                counter.clear()
                counter.update(snapshot)
            summary.clear()
            summary.update(summary_before)
            by_lifecycle.clear()
            by_lifecycle.update(lifecycle_before)
    return {"schema_version": SCHEMA, "base": str(base), "home": str(home), "created_at_epoch": now,
            "free_bytes_before": free_bytes, "pressure": pressure, "preserve": sorted(keep),
            "lease_base": str(lease_base) if lease_base else None, "evidence_leases": leases,
            "preserve_classification": [
                {"path": path, "classification": "covered-by-lease" if lease_index.covering(Path(path)) else "no-inflight-binding",
                 "lease_ids": lease_index.covering(Path(path))} for path in sorted(keep)],
            "candidate_ids": sorted(candidate_ids or ()), "registry": str(registry) if registry else None,
            "published_input_files": len(pubs.inputs), "summary": summary,
            "summary_by_lifecycle": by_lifecycle, "code_source_allocated_bytes": dict(code_source),
            "retained_logical_bytes": dict(retained), "retained_nonmedia_logical_bytes": dict(retained_nonmedia),
            "item_failures": issues.as_list(), "targets": deletes}


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def write_code_sources(base: Path, rows: list[dict]) -> None:
    """Persist where each reclaimed code copy came from before it is unlinked.

    Recorded commits come from the run itself; every file also gets its git
    blob id, so a copy with no recorded commit can still be matched in git.
    """
    by_owner: dict[str, list[dict]] = {}
    for row in rows:
        by_owner.setdefault(row["owner"], []).append(row)
    for owner, owned in by_owner.items():
        target = base / "state" / "runtime-gc-code-sources" / (hashlib.sha256(owner.encode()).hexdigest()[:24] + ".json")
        document = read_json(target)
        files = dict(document.get("files") or {})
        for row in owned:
            path = Path(row["path"])
            data = path.read_bytes()
            if identity(path.lstat()) != row["identity"]:
                raise RuntimeError(f"code copy changed while recording its source: {path}")
            files[str(path.relative_to(owner))] = {"git_blob_sha1": git_blob_sha1(data), "bytes": len(data)}
        commits = sorted(set(document.get("recorded_commits") or []).union(*(row.get("commit_hints", []) for row in owned)))
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump({"schema_version": CODE_SOURCE_SCHEMA, "run": owner, "recorded_commits": commits,
                           "files": files}, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)


def apply_plan(plan: dict) -> dict:
    """Apply only reviewed rows; recompute eligibility, then recheck each unlink."""
    if plan.get("schema_version") != SCHEMA:
        raise ValueError("unsupported lifecycle plan")
    base = Path(plan["base"])
    fresh = collect(base, home=Path(plan["home"]), preserve=set(plan["preserve"]),
                    candidate_ids=set(plan.get("candidate_ids", [])),
                    registry=Path(plan["registry"]) if plan.get("registry") else None,
                    lease_base=Path(plan["lease_base"]) if plan.get("lease_base") else None)
    if fresh["evidence_leases"] != plan.get("evidence_leases", {}):
        raise RuntimeError("evidence leases changed since collection; review a new plan")
    eligible = {row["path"]: row for row in fresh["targets"]}
    approved = plan["targets"]
    paths = [row["path"] for row in approved]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate approved target")
    for row in approved:
        current = eligible.get(row["path"])
        if not current or current["identity"] != row["identity"]:
            raise RuntimeError(f"approved target changed or protected: {row['path']}")
    # Use freshly classified owner information; the reviewed plan supplies
    # only the exact path set and its unchanged file identity.
    approved = [eligible[path] for path in paths]
    expected_inodes = {(row["identity"]["device"], row["identity"]["inode"]): dict(row["identity"])
                       for row in approved}
    hardlinked_inodes = {inode for inode, info in expected_inodes.items() if info["links"] > 1}
    inode_attributes = {}
    removed, last_owner, live = [], None, None
    self_touched: dict[str, dict[str, int]] = {}
    lease_guard = None
    try:
        lease_guard = evidence_lease.SnapshotGuard(Path(plan.get("lease_base") or base), fresh["evidence_leases"])
        if not lease_guard.unchanged():
            raise RuntimeError("evidence leases changed before apply; review a new plan")
        write_code_sources(base, [row for row in approved if row.get("kind") == "code"])
        for row in approved:
            # Refresh process/lock protection at each owner, not per media hash.
            if row["owner"] != last_owner:
                live = activity(base)
                last_owner = row["owner"]
            path = Path(row["path"])
            if path.resolve(strict=True) != path:
                raise RuntimeError(f"target parent became a symlink: {path}")
            info = path.lstat()
            inode = (row["identity"]["device"], row["identity"]["inode"])
            expected = expected_inodes[inode]
            if identity(info) != expected or path.is_symlink():
                raise RuntimeError(f"target metadata changed: {path}")
            if ((info.st_dev, info.st_ino) in live.inodes or any(under(p, Path(last_owner)) for p in live.paths)
                    or row["candidate_id"] in live.candidate_ids or row["category"] == "out" and live.runner_locked):
                raise RuntimeError(f"target became active: {path}")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC) if inode in hardlinked_inodes else None
            events = None
            try:
                before = os.fstat(fd) if fd is not None else None
                if before is not None:
                    attributes = tuple(getattr(before, key) for key in ("st_mode", "st_uid", "st_gid"))
                    if identity(before) != expected or inode in inode_attributes and attributes != inode_attributes[inode]:
                        raise RuntimeError(f"target metadata changed before unlink: {path}")
                events = UnlinkEvents(path)
                expected_parent = self_touched.get(str(path.parent), row["parent_identity"])
                if directory_stamp(path.parent.stat()) != expected_parent:
                    raise RuntimeError(f"parent directory changed since collection: {path.parent}")
                if not lease_guard.unchanged():
                    raise RuntimeError("evidence leases changed during apply; review a new plan")
                path.unlink()
                removed.append(str(path))
                parent_info = path.parent.stat()
                own_delete = events.only_our_delete()
                if own_delete is False:
                    raise RuntimeError(f"external directory write observed during unlink: {path.parent}")
                if own_delete is True:
                    self_touched[str(path.parent)] = directory_stamp(parent_info)
                if fd is not None:
                    after = os.fstat(fd)
                    observed = identity(after)
                    # Our unlink changes nlink and ctime on every alias. Bind
                    # the observed ctime only after verifying this exact inode
                    # lost one link and its contents/ownership did not change.
                    if (observed["links"] != expected["links"] - 1
                            or any(observed[key] != expected[key] for key in expected if key not in {"links", "ctime_ns"})
                            or any(getattr(after, key) != getattr(before, key) for key in ("st_mode", "st_uid", "st_gid"))):
                        raise RuntimeError(f"hard-linked inode changed during unlink: {path}")
                    expected_inodes[inode] = observed
                    inode_attributes[inode] = attributes
            finally:
                if events is not None:
                    events.close()
                if fd is not None:
                    os.close(fd)
    except Exception as exc:
        write_self_touch(base, self_touched)
        free_after = shutil.disk_usage(base).free
        return {"status": "PARTIAL" if removed else "REFUSED", "removed": removed, "error": str(exc),
                "free_bytes_after": free_after, "alert_below_10gib": free_after < 10 * GIB}
    finally:
        if lease_guard is not None:
            lease_guard.close()
    write_self_touch(base, self_touched)
    free_after = shutil.disk_usage(base).free
    return {"status": "APPLIED", "removed": removed, "free_bytes_before": fresh["free_bytes_before"],
            "free_bytes_after": free_after, "disk_delta_bytes": free_after - fresh["free_bytes_before"],
            "alert_below_10gib": free_after < 10 * GIB}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/opt/bilive/autoslice"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--preserve-json", type=Path, help="JSON array of explicitly retained paths")
    parser.add_argument("--apply-plan", type=Path, help="apply precisely this user-approved dry-run")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--proc", type=Path, default=Path("/proc"),
                        help="live process table for planning; apply always re-reads /proc")
    parser.add_argument("--lease-base", type=Path, help="explicit lease store base (offline comparisons)")
    parser.add_argument("--policy-json", type=Path, help="explicit policy for an offline read-only comparison")
    args = parser.parse_args(argv)
    if args.apply_plan:
        try:
            result = apply_plan(read_json(args.apply_plan))
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            result = {"status": "REFUSED", "removed": [], "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        if result.get("alert_below_10gib"):
            print("RUNTIME_GC_LOW_DISK: free space remains below 10 GiB", file=sys.stderr)
        return 0 if result["status"] == "APPLIED" else 1
    try:
        policy = read_json(args.policy_json or args.base / "runtime-gc.json")
        if args.policy_json and args.scheduled:
            raise ValueError("an offline policy override cannot authorize scheduled apply")
        preserve = set(policy.get("preserve", []))
        if args.preserve_json:
            preserve.update(json.loads(args.preserve_json.read_text()))
        plan = collect(args.base, home=args.home or Path(policy.get("home", "/home/user")), preserve=preserve,
                       live=activity(args.base.absolute(), args.proc), lease_base=args.lease_base)
    except (OSError, RuntimeError, ValueError) as exc:  # e.g. unreadable publication registry
        print(json.dumps({"status": "REFUSED", "removed": [], "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    if args.json:
        args.json.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    result = apply_plan(plan) if args.scheduled and policy.get("apply_enabled") is True else {
        "status": "DRY_RUN", "target_files": len(plan["targets"]), "explicit_preserve_paths": len(plan["preserve"]),
        **{key: plan[key] for key in ("schema_version", "base", "home", "created_at_epoch", "free_bytes_before", "pressure",
                                      "summary", "summary_by_lifecycle", "code_source_allocated_bytes",
                                      "retained_logical_bytes", "retained_nonmedia_logical_bytes", "item_failures",
                                      "preserve_classification")}}
    print(json.dumps(result, ensure_ascii=False))
    if result.get("alert_below_10gib"):
        print("RUNTIME_GC_LOW_DISK: free space remains below 10 GiB", file=sys.stderr)
    return 0 if result["status"] in {"DRY_RUN", "APPLIED"} else 1


