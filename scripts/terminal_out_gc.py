#!/usr/bin/env python3
"""Conservative collection of rejected-candidate source windows and caches.

This adapter deliberately owns a smaller class than the historical cleanup
planner.  It can only see an exact ``out/<date>/<candidate>`` namespace,
``candidate_rejected`` state rows, and three producer names:

* ``*_source.mp4`` / ``*_full_source.mp4``;
* a basename ending in ``source-context.context.mp4``;
* ``.context_clip_cache/<64 lower-case hex>.mp4``.

The default operation is an inventory/proof pass.  ``--apply`` repeats all
gates while holding the canonical ``runner.lock`` and then removes one closed
inode group at a time.  It never copies or hashes a recording source in full;
the recovery proof records only a size and 64 KiB edge probes, explicitly
labelled as such.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator, Mapping

try:  # Executed as ``python scripts/terminal_out_gc.py``.
    from cleanup_preflight_scan import (
        CANDIDATE_ID,
        TEXT_EXT,
        _authority_text,
        authority_references,
        candidate_states,
        in_flight_candidates,
        quiet_window,
    )
except ImportError:  # Imported as ``scripts.terminal_out_gc``.
    _SCRIPT_DIR = str(Path(__file__).resolve().parent)
    if _SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _SCRIPT_DIR)
    from cleanup_preflight_scan import (
        CANDIDATE_ID,
        TEXT_EXT,
        _authority_text,
        authority_references,
        candidate_states,
        in_flight_candidates,
        quiet_window,
    )


SCHEMA = "terminal-out-gc-plan.v1"
RECEIPT_SCHEMA = "terminal-out-gc-recovery.v1"
MAX_SOURCE_PROBE = 64 * 1024
MAX_STATE_BYTES = 64 * 2**20
DISABLED_REASON = "DISABLED flag absent — the cron runner can start a tick at any moment"
MEDIA_SUFFIXES = ("_source.mp4", "_full_source.mp4")
CONTEXT_SUFFIX = "source-context.context.mp4"
CACHE_RE = re.compile(r"^[0-9a-f]{64}\.mp4$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Keep(ValueError):
    """A fail-closed reason for one target or inode group."""


class GateError(RuntimeError):
    """The complete inventory or a mandatory host gate could not be proved."""


class RemovalStopped(Keep):
    def __init__(self, reason: str, receipt: Mapping[str, Any]):
        super().__init__(reason)
        self.receipt = dict(receipt)


def _quiet_problems(base: Path, runtime: bool) -> list[str]:
    # Runtime apply owns runner.lock before repeating all state/reference gates.
    # Only the missing DISABLED marker can be replaced by that real mutex;
    # active production processes and foreign lock owners still block.
    return [reason for reason in quiet_window(str(base))
            if not (runtime and reason == DISABLED_REASON)]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _metadata(info: os.stat_result) -> dict[str, int]:
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "mode": int(stat.S_IMODE(info.st_mode)),
        "uid": int(info.st_uid),
        "gid": int(info.st_gid),
        "bytes": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
        "nlink": int(info.st_nlink),
        "allocated_bytes": int(getattr(info, "st_blocks", 0) * 512),
    }


def _identity(meta: Mapping[str, Any], *, include_nlink: bool = True) -> tuple[int, ...]:
    keys = ("dev", "ino", "mode", "uid", "gid", "bytes", "mtime_ns", "ctime_ns")
    values = tuple(int(meta[k]) for k in keys)
    if include_nlink:
        values += (int(meta["nlink"]),)
    return values


def _same_stable(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return _identity(a) == _identity(b)


def _absolute(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _safe_open_parent(path: Path) -> tuple[int, str]:
    """Open every parent with O_NOFOLLOW and return ``(fd, basename)``."""
    path = _absolute(path)
    if ".." in path.parts:
        raise Keep(f"path traversal is not allowed: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    parent = os.open("/", flags)
    try:
        for part in path.parts[1:-1]:
            nxt = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = nxt
        return parent, path.name
    except BaseException:
        os.close(parent)
        raise


@contextmanager
def _opened_regular(path: Path) -> Iterator[tuple[int, int, str]]:
    parent, name = _safe_open_parent(path)
    fd = None
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Keep(f"not a regular file: {path}")
        yield fd, parent, name
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _lstat(path: Path) -> os.stat_result:
    return os.stat(path, follow_symlinks=False)


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        data = os.read(fd, 1024 * 1024)
        if not data:
            break
        digest.update(data)
    return digest.hexdigest()


def _snapshot_opened(fd: int, parent: int, name: str) -> dict[str, Any]:
    before = _metadata(os.fstat(fd))
    if before["nlink"] <= 0:
        raise Keep("target inode has no live links")
    digest = _hash_fd(fd)
    after = _metadata(os.fstat(fd))
    named = _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False))
    if not stat.S_ISREG(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
        raise Keep("target pathname is no longer regular")
    if not _same_stable(before, after) or not _same_stable(after, named):
        raise Keep("target changed while hashing")
    return {"schema_version": "terminal-out-gc-preimage.v1", "sha256": digest, **after}


def capture_preimage(path: Path) -> dict[str, Any]:
    with _opened_regular(path) as opened:
        return _snapshot_opened(*opened)


def _safe_text(path: Path) -> tuple[str, str | None]:
    info = _lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise GateError(f"private-runs authority file is not regular: {path}")
    return _authority_text(str(path), info)


def _out_reference_sets(base: Path, refs: Iterable[str], refdirs: Iterable[str]) -> tuple[set[str], set[str]]:
    out_root = (base / "out").resolve(strict=False)
    exact: set[str] = set()
    dirs: set[str] = set()
    for raw in refs:
        value = str(raw).rstrip(".,;:/")
        if not value.startswith("/"):
            continue
        value = os.path.normpath(value)
        if value == str(out_root) or value.startswith(str(out_root) + os.sep):
            exact.add(value)
    for raw in refdirs:
        value = str(raw).rstrip(".,;:/")
        if not value.startswith("/"):
            continue
        value = os.path.normpath(value)
        if value == str(out_root) or value.startswith(str(out_root) + os.sep):
            dirs.add(value)
    return exact, dirs


def _scan_private_runs(base: Path) -> tuple[set[str], set[str]]:
    """Scan every private-runs text document with the existing strict reader."""
    root = base / "private-runs"
    if root.is_symlink():
        raise GateError(f"private-runs root is unsafe: {root}")
    if not root.exists():
        return set(), set()
    if not root.is_dir():
        raise GateError(f"private-runs root is unsafe: {root}")
    out_root = str((base / "out").resolve(strict=False))
    pattern = re.compile(re.escape(out_root) + r"/[^\"'\s,\]\}<>()]+")
    refs: set[str] = set()
    refdirs: set[str] = set()
    stack = [root]
    seen_dirs: set[tuple[int, int]] = set()
    observed: dict[Path, tuple[int, ...]] = {}
    observed_kind: dict[Path, str] = {}
    links: list[dict[str, Any]] = []
    deferred_links: list[dict[str, Any]] = []
    missing_mp4_targets: list[Path] = []

    def remember(path: Path, info: os.stat_result) -> None:
        observed[path] = _identity(_metadata(info))
        if stat.S_ISDIR(info.st_mode):
            observed_kind[path] = "directory"
        elif stat.S_ISREG(info.st_mode):
            observed_kind[path] = "file"
        elif stat.S_ISLNK(info.st_mode):
            observed_kind[path] = "symlink"
        else:
            observed_kind[path] = "other"

    def lexical_target(path: Path, link_value: str) -> Path | None:
        """Resolve a relative link lexically, without following any component."""
        if os.path.isabs(link_value):
            return None
        try:
            relative_parent = list(path.parent.relative_to(root).parts)
        except ValueError:
            return None
        components = list(relative_parent)
        for component in Path(link_value).parts:
            if component in ("", "."):
                continue
            if component == "..":
                if not components:
                    return None
                components.pop()
            else:
                components.append(component)
        return root.joinpath(*components)

    def ordinary_scanned_ancestors(path: Path) -> bool:
        """Require every canonical parent to be an observed ordinary directory."""
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            return False
        current = root
        if observed_kind.get(current) != "directory":
            return False
        for part in parts[:-1]:
            current /= part
            if observed_kind.get(current) != "directory":
                return False
        return True

    def is_same_basename_mp4_alias(path: Path, target: Path) -> bool:
        return (
            path.suffix.lower() == ".mp4"
            and target.suffix.lower() == ".mp4"
            and path.name == target.name
        )

    while stack:
        directory = stack.pop()
        info = _lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise GateError(f"private-runs directory is not a directory: {directory}")
        key = (int(info.st_dev), int(info.st_ino))
        if key in seen_dirs:
            raise GateError(f"private-runs directory alias/cycle: {directory}")
        seen_dirs.add(key)
        remember(directory, info)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise GateError(f"private-runs directory unreadable: {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            entry_info = entry.stat(follow_symlinks=False)
            remember(path, entry_info)
            if stat.S_ISLNK(entry_info.st_mode):
                try:
                    link_value = os.readlink(path)
                except OSError as exc:
                    raise GateError(f"private-runs link unreadable: {path}: {exc}") from exc
                links.append({
                    "path": path,
                    "value": link_value,
                    "identity": observed[path],
                })
                # Private workers share the registered interpreter. It is an
                # infrastructure alias, not a private source/authority tree.
                # Keep its link identity in the scan, and never traverse it.
                if path.name == "venv-main" and path.resolve(strict=True) == base / "venv-main" and (base / "venv-main").is_dir():
                    continue
                deferred_links.append({
                    "path": path,
                    "value": link_value,
                    "identity": observed[path],
                    "target": lexical_target(path, link_value),
                })
                continue
            if stat.S_ISDIR(entry_info.st_mode):
                stack.append(path)
                continue
            if not stat.S_ISREG(entry_info.st_mode):
                raise GateError(f"private-runs contains unsafe non-file: {path}")
            if path.suffix.lower() not in TEXT_EXT:
                continue
            text, companion = _safe_text(path)
            refs.update(hit.rstrip(".,;:/") for hit in pattern.findall(text))
            if companion is not None:
                companion_path = path.parent / companion
                if not companion_path.exists() or companion_path.is_symlink():
                    raise GateError(f"AppleDouble companion missing or unsafe: {companion_path}")
                if companion_path.suffix.lower() not in TEXT_EXT:
                    raise GateError(f"AppleDouble companion is outside text scope: {companion_path}")
                companion_info = _lstat(companion_path)
                companion_text, nested = _safe_text(companion_path)
                if nested is not None:
                    raise GateError(f"nested AppleDouble companion is unsupported: {companion_path}")
                refs.update(hit.rstrip(".,;:/") for hit in pattern.findall(companion_text))
                if not stat.S_ISREG(companion_info.st_mode):
                    raise GateError(f"AppleDouble companion is not regular: {companion_path}")
                remember(companion_path, companion_info)
    for alias in deferred_links:
        path = alias["path"]
        target = alias["target"]
        if target is None:
            raise GateError(f"private-runs contains unsafe link: {path} (absolute or escapes root)")
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise GateError(f"private-runs contains unsafe link: {path} (escapes root)") from exc
        if not ordinary_scanned_ancestors(target):
            raise GateError(f"private-runs contains unsafe link: {path} (target has an unscanned or linked ancestor)")
        try:
            target_info = _lstat(target)
        except FileNotFoundError:
            if not is_same_basename_mp4_alias(path, target):
                raise GateError(f"private-runs contains unsafe link: {path} (directory target is missing)")
            missing_mp4_targets.append(target)
            continue
        except OSError as exc:
            raise GateError(f"private-runs contains unsafe link: {path} (target unreadable: {exc})") from exc
        if stat.S_ISLNK(target_info.st_mode):
            raise GateError(f"private-runs contains unsafe link: {path} (target is another link)")
        expected_target = observed.get(target)
        if expected_target is None:
            raise GateError(f"private-runs contains unsafe link: {path} (target was not scanned)")
        actual_target = _identity(_metadata(target_info))
        if actual_target != expected_target:
            raise GateError(f"private-runs contains unsafe link: {path} (target changed during scan)")
        target_kind = observed_kind.get(target)
        if stat.S_ISDIR(target_info.st_mode):
            if target_kind != "directory":
                raise GateError(f"private-runs contains unsafe link: {path} (directory target was not fully scanned)")
        elif stat.S_ISREG(target_info.st_mode):
            if target_kind != "file" or not is_same_basename_mp4_alias(path, target):
                raise GateError(f"private-runs contains unsafe link: {path} (not an internal same-basename MP4 alias)")
        else:
            raise GateError(f"private-runs contains unsafe link: {path} (target is not an ordinary file or directory)")
        alias["target_identity"] = expected_target
    for path, expected in observed.items():
        try:
            actual = _identity(_metadata(_lstat(path)))
        except FileNotFoundError as exc:
            raise GateError(f"private-runs path disappeared during scan: {path}") from exc
        if actual != expected:
            raise GateError(f"private-runs path changed during scan: {path}")
    for alias in links:
        path = alias["path"]
        try:
            if os.readlink(path) != alias["value"]:
                raise GateError(f"private-runs link value changed during scan: {path}")
        except FileNotFoundError as exc:
            raise GateError(f"private-runs link disappeared during scan: {path}") from exc
        except OSError as exc:
            raise GateError(f"private-runs link unreadable after scan: {path}: {exc}") from exc
    for target in missing_mp4_targets:
        try:
            _lstat(target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GateError(f"private-runs missing MP4 alias target unreadable after scan: {target}: {exc}") from exc
        raise GateError(f"private-runs missing MP4 alias target appeared during scan: {target}")
    for path in refs:
        if not os.path.splitext(path)[1] and len(Path(path).relative_to(out_root).parts) >= 3:
            refdirs.add(path)
    return refs, refdirs


def _walk_out(base: Path) -> tuple[dict[tuple[int, int], list[Path]], list[str]]:
    """Return the full regular-file inode census under out, without links."""
    root = base / "out"
    if not root.exists() or root.is_symlink() or not root.is_dir():
        raise GateError(f"out root is missing or unsafe: {root}")
    aliases: dict[tuple[int, int], list[Path]] = {}
    errors: list[str] = []
    stack = [root]
    visited_dirs: set[tuple[int, int]] = set()
    root_dev = int(_lstat(root).st_dev)
    while stack:
        directory = stack.pop()
        info = _lstat(directory)
        dkey = (int(info.st_dev), int(info.st_ino))
        if dkey in visited_dirs:
            errors.append(f"directory alias/cycle: {directory}")
            continue
        visited_dirs.add(dkey)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            errors.append(f"out directory unreadable: {directory}: {exc}")
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                item = entry.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append(f"out entry unreadable: {path}: {exc}")
                continue
            if stat.S_ISLNK(item.st_mode):
                errors.append(f"out contains unsafe link: {path}")
            elif stat.S_ISDIR(item.st_mode):
                if int(item.st_dev) != root_dev:
                    errors.append(f"out crosses filesystem: {path}")
                else:
                    stack.append(path)
            elif stat.S_ISREG(item.st_mode):
                aliases.setdefault((int(item.st_dev), int(item.st_ino)), []).append(path)
            else:
                errors.append(f"out contains unsafe non-file: {path}")
    return aliases, errors


def _candidate_target(path: Path, out_root: Path) -> tuple[str, str, str] | None:
    try:
        rel = path.relative_to(out_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 3 or not DATE_RE.fullmatch(parts[0]):
        return None
    candidate = parts[1]
    if re.fullmatch(CANDIDATE_ID.pattern, candidate) is None:
        return None
    name = path.name
    if (len(parts) == 10 and parts[2] in {'song_selector', 'song_selector_full'}
            and parts[3].startswith('attempt-') and re.fullmatch(r'seededsong_\d+_\d+', parts[4])
            and parts[5:7] == ('song_repair', 'agy_audio_lrc')
            and re.fullmatch(r'variant-\d+', parts[7]) and parts[8].startswith(parts[4] + '-')
            and name == 'input.mp4'):
        return parts[0], candidate, 'agy-audio-lrc-input'
    if name.endswith(MEDIA_SUFFIXES):
        return parts[0], candidate, "source-window"
    if name.endswith(CONTEXT_SUFFIX):
        return parts[0], candidate, "source-context"
    if len(parts) == 4 and parts[2] == ".context_clip_cache" and CACHE_RE.fullmatch(name):
        return parts[0], candidate, "context-cache"
    return None


def _read_json_regular(path: Path, *, limit: int = MAX_STATE_BYTES) -> dict[str, Any]:
    with _opened_regular(path) as (fd, parent, name):
        meta = _metadata(os.fstat(fd))
        if meta["bytes"] > limit:
            raise GateError(f"JSON file exceeds read limit: {path}")
        raw = os.read(fd, limit + 1)
        if len(raw) != meta["bytes"]:
            raise GateError(f"JSON file changed while read: {path}")
        after = _metadata(os.fstat(fd))
        if not _same_stable(meta, after):
            raise GateError(f"JSON file changed while read: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateError(f"state JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise GateError(f"state JSON is not an object: {path}")
    return value


def _state_rows(base: Path) -> tuple[dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], Path]]], list[str]]:
    root = base / "state"
    rows: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], Path]]] = {}
    errors: list[str] = []
    if not root.exists():
        return rows, [f"state root missing: {root}"]
    if root.is_symlink() or not root.is_dir():
        return rows, [f"state root unsafe: {root}"]
    for path in sorted(root.glob("2026-*.json")):
        if ".pre-" in path.name:
            continue
        try:
            doc = _read_json_regular(path)
        except (OSError, GateError) as exc:
            errors.append(str(exc))
            continue
        date = path.stem
        for collection in ("songs", "picks"):
            value = doc.get(collection)
            if value is None:
                continue
            if not isinstance(value, list):
                errors.append(f"state {path} collection {collection} is not a list")
                continue
            for row in value:
                if not isinstance(row, dict):
                    errors.append(f"state {path} has non-object {collection} row")
                    continue
                cid = str(row.get("candidate_id") or row.get("cid") or "").strip()
                if cid:
                    rows.setdefault((date, cid), []).append((row, doc, path))
    return rows, errors


def _positive_int(value: Any) -> int | None:
    return int(value) if type(value) is int and value > 0 else None


def _known_segment_metadata(row: Mapping[str, Any], doc: Mapping[str, Any], basename: str) -> tuple[int | None, int | None]:
    """Read metadata bound to this segment, never a whole-day aggregate."""
    stem = Path(basename).stem
    sizes: list[int] = []
    durations: list[int] = []
    for key in ("segment_size_bytes", "source_size_bytes", "segment_bytes", "source_bytes"):
        if (value := _positive_int(row.get(key))) is not None:
            sizes.append(value)
    for key in ("segment_duration_ms", "source_duration_ms", "seg_dur_ms"):
        if (value := _positive_int(row.get(key))) is not None:
            durations.append(value)

    durations_map = doc.get("segment_durations_ms")
    if isinstance(durations_map, Mapping):
        for key in (stem, basename):
            if (value := _positive_int(durations_map.get(key))) is not None:
                durations.append(value)
                break
    scene_map = doc.get("segment_scene_contexts")
    if isinstance(scene_map, Mapping):
        scene = scene_map.get(stem) or scene_map.get(basename)
        if isinstance(scene, Mapping):
            signatures = scene.get("input_signatures")
            segment_sig = signatures.get("segment") if isinstance(signatures, Mapping) else None
            if isinstance(segment_sig, Mapping):
                if (value := _positive_int(segment_sig.get("size_bytes"))) is not None:
                    sizes.append(value)

    inventory = doc.get("source_integrity")
    if isinstance(inventory, Mapping):
        # A source-integrity segment table may carry exact per-segment values;
        # select the matching basename before reading any fields.
        for container_key in ("segments", "segment_inventory", "observations", "consumer_segments"):
            container = inventory.get(container_key)
            children = (
                container.values() if isinstance(container, Mapping)
                else container if isinstance(container, list) else ()
            )
            for child in children:
                if not isinstance(child, Mapping):
                    continue
                child_path = child.get("path") or child.get("segment") or child.get("segment_path")
                if not isinstance(child_path, str) or Path(child_path).name != basename:
                    continue
                for key in ("size_bytes", "segment_size_bytes", "source_size_bytes"):
                    if (value := _positive_int(child.get(key))) is not None:
                        sizes.append(value)
                        break
                for key in ("duration_ms", "segment_duration_ms", "source_duration_ms"):
                    if (value := _positive_int(child.get(key))) is not None:
                        durations.append(value)
                        break
    return (
        sizes[0] if sizes and all(value == sizes[0] for value in sizes) else None,
        durations[0] if durations and all(value == durations[0] for value in durations) else None,
    )


def _source_root_allowed(base: Path, source: Path) -> bool:
    source = _absolute(source)
    configured_roots = []
    for value in os.environ.get("AUTOSLICE_GC_SOURCE_ROOTS", "").split(os.pathsep):
        if not value:
            continue
        root = Path(value)
        if not root.is_absolute() or ".." in root.parts or root == Path("/"):
            raise Keep("GC source roots must be absolute non-root paths without traversal")
        configured_roots.append(root)
    for local_root_name in ("recordings", "recording", "sources"):
        try:
            if source.is_relative_to(base / local_root_name):
                return True
        except ValueError:
            pass
    return any(source.is_relative_to(root) for root in configured_roots)


def _source_path(row: Mapping[str, Any], inventory: Mapping[str, Any], base: Path, date: str) -> Path | None:
    raw = row.get("segment") or row.get("segment_path") or row.get("source_segment")
    if not isinstance(raw, str) or not raw.strip():
        return None
    date_dir_raw = inventory.get("date_dir")
    if not isinstance(date_dir_raw, str) or not date_dir_raw:
        return None
    date_dir = _absolute(date_dir_raw)
    if date_dir.name != date or not date_dir.is_dir() or date_dir.is_symlink():
        return None
    candidate = _absolute(raw) if Path(raw).is_absolute() else date_dir / Path(raw).name
    if candidate.parent != date_dir or candidate.suffix.lower() != ".mp4":
        candidate = date_dir / candidate.name
    if candidate.parent != date_dir or not _source_root_allowed(base, candidate):
        return None
    consumer = inventory.get("consumer_segments")
    if not isinstance(consumer, list) or not consumer:
        return None
    matched = []
    for item in consumer:
        if isinstance(item, Mapping):
            item_path = item.get("path") or item.get("segment")
        else:
            item_path = item
        if isinstance(item_path, str) and Path(item_path).name == candidate.name:
            matched.append(_absolute(item_path) if Path(item_path).is_absolute() else date_dir / Path(item_path).name)
    if len(matched) != 1 or matched[0] != candidate or not candidate.exists() or candidate.is_symlink():
        return None
    return candidate


def _probe_source(path: Path, *, expected_size: int, expected_duration_ms: int) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise Keep(f"source path is unsafe: {path}")
    with _opened_regular(path) as (fd, parent, name):
        before = _metadata(os.fstat(fd))
        if before["bytes"] != expected_size:
            raise Keep(f"source size differs from sealed state: {path}")
        head_len = min(MAX_SOURCE_PROBE, before["bytes"])
        head = os.pread(fd, head_len, 0) if hasattr(os, "pread") else _read_edge(fd, 0, head_len)
        tail_offset = max(0, before["bytes"] - MAX_SOURCE_PROBE)
        tail_len = min(MAX_SOURCE_PROBE, before["bytes"])
        tail = os.pread(fd, tail_len, tail_offset) if hasattr(os, "pread") else _read_edge(fd, tail_offset, tail_len)
        after = _metadata(os.fstat(fd))
        named = _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False))
        if not _same_stable(before, after) or not _same_stable(after, named):
            raise Keep(f"recording source changed during edge probe: {path}")
    return {
        "path": str(path),
        "size_bytes": before["bytes"],
        "duration_ms": expected_duration_ms,
        "metadata": before,
        "head_probe": {"bytes": len(head), "sha256": hashlib.sha256(head).hexdigest()},
        "tail_probe": {"bytes": len(tail), "sha256": hashlib.sha256(tail).hexdigest()},
        "probe_scope": "first_and_last_65536_bytes_only; not_full_hash",
    }


def _read_edge(fd: int, offset: int, length: int) -> bytes:
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def _issue_matches_source(issue: Mapping[str, Any], source: Path) -> bool:
    source_text = str(source)
    source_name = source.name
    source_stem = source.stem
    for key in ("path", "raw_media_path", "segment_path", "source_path"):
        value = issue.get(key)
        if isinstance(value, str) and (os.path.normpath(value) == source_text or Path(value).name == source_name):
            return True
    for key in ("segment_stem", "stem", "segment"):
        value = issue.get(key)
        if isinstance(value, str) and Path(value).stem == source_stem:
            return True
    return False


def _target_source_issue(inventory: Mapping[str, Any], source: Path) -> str | None:
    issues = inventory.get("issues")
    if not isinstance(issues, list):
        return None if issues is None else "malformed source_integrity issues"
    blocking = {"BLOCK", "ERROR", "FAIL", "FAILED", "CRITICAL"}
    for issue in issues:
        if not isinstance(issue, Mapping):
            return "malformed source_integrity issue"
        severity = str(issue.get("severity") or "BLOCK").upper()
        if severity not in blocking:
            continue
        # A global blocking issue applies to the sealed date.  A warning on an
        # unrelated discarded stub does not; a blocking issue tied to this
        # recording does.
        if not any(key in issue for key in ("path", "raw_media_path", "segment_path", "source_path", "segment_stem", "stem", "segment")) or _issue_matches_source(issue, source):
            return str(issue.get("code") or issue.get("message") or "source_integrity blocking issue")
    return None


def _row_int(row: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _recovery_intervals(row: Mapping[str, Any]) -> dict[str, Any]:
    timeline = row.get("timeline") if isinstance(row.get("timeline"), Mapping) else {}
    window = row.get("window") if isinstance(row.get("window"), Mapping) else {}
    context = row.get("context") if isinstance(row.get("context"), Mapping) else {}
    start = _row_int(row, "start_ms", "anchor_start_ms", "source_start_ms")
    end = _row_int(row, "end_ms", "anchor_end_ms", "source_end_ms")
    if start is None:
        start = _row_int(window, "start_ms", "anchor_start_ms", "source_start_ms")
    if end is None:
        end = _row_int(window, "end_ms", "anchor_end_ms", "source_end_ms")
    if start is None:
        start = _row_int(timeline, "start_ms", "anchor_start_ms", "source_start_ms")
    if end is None:
        end = _row_int(timeline, "end_ms", "anchor_end_ms", "source_end_ms")
    context_start = _row_int(row, "context_start_ms")
    context_end = _row_int(row, "context_end_ms")
    context_duration = _row_int(row, "context_duration_ms")
    if context_start is None:
        context_start = _row_int(context, "context_start_ms", "start_ms")
    if context_end is None:
        context_end = _row_int(context, "context_end_ms", "end_ms")
    if context_duration is None:
        context_duration = _row_int(context, "context_duration_ms", "duration_ms")
    if context_start is None:
        context_start = _row_int(timeline, "context_start_ms", "start_ms")
    if context_end is None:
        context_end = _row_int(timeline, "context_end_ms", "end_ms")
    if context_duration is None:
        context_duration = _row_int(timeline, "context_duration_ms", "duration_ms")
    if context_end is None and context_start is not None and context_duration is not None:
        context_end = context_start + context_duration
    if context_duration is None and context_start is not None and context_end is not None:
        context_duration = context_end - context_start
    return {
        "window": {"start_ms": start, "end_ms": end},
        "context": {"start_ms": context_start, "end_ms": context_end, "duration_ms": context_duration},
    }


def _valid_interval(interval: Mapping[str, Any], *, with_duration: bool = False) -> bool:
    start = interval.get("start_ms")
    end = interval.get("end_ms")
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        return False
    if with_duration:
        duration = interval.get("duration_ms")
        return type(duration) is int and duration > 0 and duration == end - start
    return True


def _rebuild_method(candidate: str, intervals: Mapping[str, Any], categories: Iterable[str]) -> dict[str, Any]:
    categories = set(categories)
    methods: list[dict[str, Any]] = []
    if "source-window" in categories:
        methods.append({
            "producer": "src.autoslice.song_lane.song_window_media_path",
            "purpose": "rebuild interval-bound source window from retained recording",
            "candidate_id": candidate,
            "window": intervals["window"],
        })
    if "source-context" in categories:
        methods.append({
            "producer": "src.autoslice.source_context_executor.execute_source_context_job",
            "purpose": "rebuild source-context.context.mp4 from retained recording and manifest timeline",
            "candidate_id": candidate,
            "context": intervals["context"],
        })
    if "context-cache" in categories:
        methods.append({
            "producer": "src.autoslice.source_context_executor._context_clip_cache_key",
            "purpose": "recreate cache key and republish the context clip after source-context rebuild",
            "candidate_id": candidate,
            "context": intervals["context"],
        })
    return {"methods": methods, "evidence_scope": "recipe and state binding; does not claim target bytes are reproducible without executing the producer"}


def _context_recipe(base: Path, date: str, candidate: str, row: Mapping[str, Any], paths: Iterable[Path]) -> dict[str, Any]:
    namespace = base / 'out' / date / candidate
    cache_paths = [p for p in paths if _candidate_target(p, base / 'out')[2] == 'context-cache']
    recipes = []
    for path in paths:
        if _candidate_target(path, base / 'out')[2] != 'source-context':
            continue
        summary_path = path.parents[2] / 'summary.json'
        if not summary_path.is_relative_to(namespace):
            raise Keep('context summary is outside owner namespace')
        summary = _read_json_regular(summary_path)
        matches = [record for record in summary.get('records', [])
                   if isinstance(record, dict) and record.get('source_context', {}).get('context_media_path') == str(path)]
        if len(matches) != 1:
            raise Keep('context output lacks one exact completed summary binding')
        record = matches[0]
        context = record['source_context']
        if context.get('decision') != 'READY' or context.get('jingting_done') is not True:
            raise Keep('context attempt is not complete')
        timeline = record.get('source_context_job', {}).get('timeline', {})
        interval = {'start_ms': timeline.get('context_start_ms'), 'end_ms': timeline.get('context_end_ms'), 'duration_ms': timeline.get('context_duration_ms')}
        if not _valid_interval(interval, with_duration=True):
            raise Keep('context summary has no exact recovery timeline')
        source = Path(str(summary.get('input', {}).get('source_video') or ''))
        match = re.fullmatch(re.escape(candidate) + r'_(tight|full)_(\d+)_(\d+)_source\.mp4', source.name)
        if source.parent != namespace or not match:
            raise Keep('context input is not an interval-bound owner source window')
        start, end = int(match[2]), int(match[3])
        expected = row if match[1] == 'tight' else row.get('full_source_retry', {})
        if expected.get('start_ms') != start or expected.get('end_ms') != end:
            raise Keep('context source window differs from exact owner state interval')
        ledger = summary.get('source_integrity', {}).get('ledger', {})
        if ledger.get('can_use_local_source') is not True or ledger.get('issues') or timeline.get('source_duration_ms') != end - start:
            raise Keep('context source duration/health is not bound to source window')
        if interval['end_ms'] > end - start:
            raise Keep('context extends beyond source window')
        manifest_path = Path(str(context.get('jingting_manifest_path') or ''))
        if manifest_path.parent != path.parent:
            raise Keep('context manifest is outside exact job')
        manifest = _read_json_regular(manifest_path)
        source_sha = str(manifest.get('source_sha256') or '').removeprefix('sha256:')
        if not re.fullmatch(r'[0-9a-f]{64}', source_sha) or manifest.get('source_offset_ms') != interval['start_ms']:
            raise Keep('context manifest source/offset binding invalid')
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', f"{interval['start_ms']/1000:.3f}", '-i', str(source), '-t', f"{interval['duration_ms']/1000:.3f}", '-c', 'copy', '<output>']
        payload = {'source_sha256': source_sha, 'context_start_ms': interval['start_ms'], 'context_duration_ms': interval['duration_ms'], 'command': command}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if cache_paths and (len(cache_paths) != 1 or cache_paths[0].stem != key):
            raise Keep('context cache key differs from bound producer recipe')
        recipes.append({'context': interval, 'source_window': {'path': str(source), 'start_ms': start, 'end_ms': end, 'sha256': source_sha}, 'cache_key': key, 'command': command,
                        'summary': {'path': str(summary_path), 'preimage': capture_preimage(summary_path)},
                        'manifest': {'path': str(manifest_path), 'preimage': capture_preimage(manifest_path)}})
    if not recipes:
        raise Keep('context cache has no completed source-context alias')
    identities = {(json.dumps(r['context'], sort_keys=True), json.dumps(r['source_window'], sort_keys=True), r['cache_key']) for r in recipes}
    if len(identities) != 1:
        raise Keep('context aliases disagree on producer input/timeline')
    return {'context': recipes[0]['context'], 'source_window': recipes[0]['source_window'], 'cache_key': recipes[0]['cache_key'], 'attempt_receipts': recipes}


def _source_recovery_proof(base: Path, date: str, candidate: str, rows: list[tuple[dict[str, Any], dict[str, Any], Path]], categories: Iterable[str] = (), paths: Iterable[Path] = ()) -> dict[str, Any]:
    if len(rows) != 1:
        raise Keep("candidate owner row is missing or ambiguous")
    row, doc, state_path = rows[0]
    if row.get("status") != "candidate_rejected":
        raise Keep("owner row is not candidate_rejected")
    inventory = doc.get("source_integrity")
    if not isinstance(inventory, Mapping):
        raise Keep("source_integrity is missing")
    if inventory.get("status") != "PASS" or inventory.get("can_select") is not True:
        raise Keep("source_integrity is not a sealed PASS")
    source = _source_path(row, inventory, base, date)
    if source is None:
        raise Keep("recording source is missing or not bound by consumer_segments")
    if (issue := _target_source_issue(inventory, source)) is not None:
        raise Keep(f"source_integrity blocks selected recording: {issue}")
    size, duration = _known_segment_metadata(row, doc, source.name)
    if size is None or duration is None:
        raise Keep("state lacks unique known source size and duration metadata")
    intervals = _recovery_intervals(row)
    category_set = set(categories)
    paths = list(paths)
    window_paths = [p for p in paths if _candidate_target(p, base / 'out')[2] == 'source-window']
    if window_paths:
        if len(window_paths) != 1:
            raise Keep('source inode has multiple distinct owner windows')
        window = window_paths[0]
        match = re.fullmatch(re.escape(candidate) + r'_(tight|full)_(\d+)_(\d+)_source\.mp4', window.name)
        if not match:
            raise Keep('source window has no exact producer interval binding')
        if match:
            expected = row if match[1] == 'tight' else row.get('full_source_retry', {})
            if (expected.get('start_ms'), expected.get('end_ms')) != (int(match[2]), int(match[3])):
                raise Keep('source window filename disagrees with exact owner interval')
            intervals['window'] = {'start_ms': int(match[2]), 'end_ms': int(match[3])}
    agy_recipes: list[dict[str, Any]] = []
    if 'agy-audio-lrc-input' in category_set:
        if len(window_paths) != 1:
            raise Keep('AGY input lacks exact source-window alias')
        source_window = str(window_paths[0])
        agy_paths = [p for p in paths if _candidate_target(p, base / 'out')[2] == 'agy-audio-lrc-input']
        for agy in agy_paths:
            manifest_path = agy.parent / 'run.manifest.json'
            manifest = _read_json_regular(manifest_path)
            artifacts = manifest.get('artifacts')
            if not isinstance(artifacts, Mapping):
                raise Keep('AGY source alias lacks matching completed producer receipt')
            if (
                manifest.get('schema_version') != 'agy-audio-lrc-run.v3'
                or type(manifest.get('agy_rc')) is not int
                or manifest.get('agy_rc') != 0
                or not manifest.get('finished_at')
                or artifacts.get('source_path') != str(agy)
                or artifacts.get('source_origin_path') != source_window
            ):
                raise Keep('AGY source alias lacks matching completed producer receipt')
            output = Path(str(artifacts.get('output_path') or ''))
            output_sha = artifacts.get('output_sha256')
            if (output.parent != agy.parent or not isinstance(output_sha, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', output_sha)
                    or capture_preimage(output)['sha256'] != output_sha):
                raise Keep('AGY completed output hash/path invalid')
            source_sha = artifacts.get('source_sha256')
            if not isinstance(source_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha):
                raise Keep('AGY input content hash missing')
            agy_recipes.append({
                'source_sha256': source_sha,
                'source_window': source_window,
                'input': str(agy),
                'manifest': {'path': str(manifest_path), 'preimage': capture_preimage(manifest_path)},
                'output': {'path': str(output), 'preimage': capture_preimage(output)},
            })
        if not agy_recipes:
            raise Keep('AGY input alias is missing')
        if len({recipe['source_sha256'] for recipe in agy_recipes}) != 1:
            raise Keep('AGY producer input hashes disagree')
    if "source-window" in category_set and not _valid_interval(intervals["window"]):
        raise Keep("source-window recovery interval is missing from owner state")
    context_recipe = None
    context_only_group = len(paths) > 1 and category_set == {'source-context'}
    if ({"source-context", "context-cache"} & category_set) and (context_only_group or not _valid_interval(intervals["context"], with_duration=True)):
        context_recipe = _context_recipe(base, date, candidate, row, list(paths))
        intervals["context"] = context_recipe["context"]
        intervals["window"] = {key: context_recipe["source_window"][key] for key in ("start_ms", "end_ms")}
    if (window_paths or context_recipe) and (not _valid_interval(intervals['window']) or intervals['window']['end_ms'] > duration):
        raise Keep('producer window exceeds the known recording duration')
    probe = _probe_source(source, expected_size=size, expected_duration_ms=duration)
    return {
        "candidate_id": candidate,
        "recording_date": date,
        "state_path": str(state_path),
        "segment": row.get("segment") or row.get("segment_path"),
        "source_integrity": {
            "date_dir": str(inventory.get("date_dir")),
            "consumer_segments": list(inventory.get("consumer_segments") or []),
            "status": inventory.get("status"),
        },
        "intervals": intervals,
        "context_recipe": context_recipe,
        # Keep the old singular field for one completed AGY attempt.  A group
        # with multiple attempts must consume the complete list instead of a
        # lossy representative.
        "agy_recipe": agy_recipes[0] if len(agy_recipes) == 1 else None,
        "agy_recipes": agy_recipes,
        "rebuild": _rebuild_method(candidate, intervals, category_set),
        "recording": probe,
    }


def _group_preimages(paths: list[Path]) -> list[dict[str, Any]]:
    # One closed hardlink group has one byte stream. Hash it once, then bind
    # every alias to that same stable inode without rereading gigabytes per retry.
    with _opened_regular(paths[0]) as first_open:
        first = _snapshot_opened(*first_open)
        for path in paths[1:]:
            with _opened_regular(path) as opened:
                fd, parent, name = opened
                meta = _metadata(os.fstat(fd))
                named = _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False))
                if not _same_stable(meta, first) or not _same_stable(named, meta):
                    raise Keep("hardlink group changed during preimage capture")
        if not _same_stable(_metadata(os.fstat(first_open[0])), first):
            raise Keep("hardlink inode changed after hashing")
    return [dict(first) for _ in paths]


def _candidate_namespaces(groups: Iterable[Mapping[str, Any]]) -> list[Path]:
    return sorted({Path(str(group["namespace"])) for group in groups})


def _proc_census(base: Path, targets: Iterable[tuple[int, int]], namespaces: Iterable[Path]) -> dict[str, Any]:
    """Census target inodes plus every visible command/cwd namespace on Linux."""
    target_set = {(int(dev), int(ino)) for dev, ino in targets}
    wanted = [str(path) for path in namespaces]
    proc = Path("/proc")
    if not proc.is_dir():
        return {"available": False, "reason": "proc_unavailable_non_linux", "open_inodes": [], "active_namespaces": [], "processes": []}
    processes: list[dict[str, Any]] = []
    open_inodes: list[dict[str, Any]] = []
    active_namespaces: list[dict[str, Any]] = []
    try:
        entries = sorted(proc.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise GateError(f"/proc enumeration failed: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        try:
            cmd_raw = (entry / "cmdline").read_bytes()
            cwd = os.readlink(entry / "cwd")
            cmd = cmd_raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            record = {"pid": pid, "cmd": cmd, "cwd": cwd}
            processes.append(record)
            if any(cwd == ns or cwd.startswith(ns + os.sep) or ns in cmd for ns in wanted):
                active_namespaces.append(record)
            fd_dir = entry / "fd"
            for fd_entry in sorted(fd_dir.iterdir(), key=lambda item: item.name):
                try:
                    info = os.stat(fd_entry, follow_symlinks=True)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError as exc:
                    raise GateError(f"/proc/{pid}/fd visibility denied") from exc
                identity = (int(info.st_dev), int(info.st_ino))
                if identity in target_set:
                    open_inodes.append({"pid": pid, "fd": fd_entry.name, "dev": identity[0], "ino": identity[1]})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise GateError(f"/proc/{pid} visibility denied") from exc
        except OSError as exc:
            # A process can disappear, but an existing process entry failing for
            # any other reason leaves the namespace census incomplete.
            if entry.exists():
                raise GateError(f"/proc/{pid} census failed: {exc}") from exc
    return {
        "available": True,
        "open_inodes": open_inodes,
        "active_namespaces": active_namespaces,
        "processes": processes,
    }


def _cited(path: Path, refs: set[str], refdirs: set[str]) -> bool:
    text = str(path)
    return text in refs or any(text == directory or text.startswith(directory + os.sep) for directory in refdirs)


def _group_allowed(aliases: list[Path], out_root: Path, candidate: str, date: str) -> tuple[bool, str]:
    if not aliases:
        return False, "inode_has_no_aliases"
    try:
        inode_nlink = int(_lstat(aliases[0]).st_nlink)
    except OSError:
        return False, "inode_stat_failed"
    if inode_nlink != len(aliases):
        return False, "inode_alias_outside_known_set"
    categories = []
    for path in aliases:
        target = _candidate_target(path, out_root)
        if target is None:
            return False, "inode_alias_not_allowed"
        p_date, p_candidate, category = target
        if p_date != date or p_candidate != candidate:
            return False, "inode_alias_owner_mismatch"
        categories.append(category)
    context_group = (categories.count('context-cache') == 1 and categories.count('source-context') == len(aliases) - 1) or all(c == 'source-context' for c in categories)
    # AGY may leave more than one completed input attempt sharing the exact
    # source-window inode.  Keep the group closed and require one window plus
    # at least one AGY input; unknown aliases are still rejected above.
    agy_group = (
        categories.count('source-window') == 1
        and categories.count('agy-audio-lrc-input') >= 1
        and len(categories) == categories.count('source-window') + categories.count('agy-audio-lrc-input')
    )
    if len(aliases) > 1 and not (context_group or agy_group):
        return False, "generic_hardlink_group_not_allowed"
    return True, "ok"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = _absolute(path)
    if path.exists() and path.is_symlink():
        raise GateError(f"receipt path is a symlink: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _group_receipt_path(state_dir: Path, group: Mapping[str, Any]) -> Path:
    digest = hashlib.sha256(
        json.dumps({"paths": group["paths"], "preimages": group["preimages"]}, sort_keys=True).encode()
    ).hexdigest()
    return state_dir / "recoveries" / "terminal-out" / f"{digest}.json"


def _verify_source_again(proof: Mapping[str, Any]) -> None:
    recording = proof.get("recording")
    if not isinstance(recording, Mapping):
        raise Keep("source recovery proof missing recording")
    path = Path(str(recording.get("path") or ""))
    expected = int(recording.get("size_bytes") or 0)
    duration = int(recording.get("duration_ms") or 0)
    fresh = _probe_source(path, expected_size=expected, expected_duration_ms=duration)
    if fresh["size_bytes"] != recording.get("size_bytes") or fresh["head_probe"] != recording.get("head_probe") or fresh["tail_probe"] != recording.get("tail_probe"):
        raise Keep("recording source edge proof drifted")


def _apply_group(group: Mapping[str, Any], base: Path, state_dir: Path) -> dict[str, Any]:
    paths = [Path(str(path)) for path in group["paths"]]
    allowed, reason = _group_allowed(paths, base / "out", str(group["owner"]), str(group["date"]))
    if not allowed:
        raise Keep(reason)
    opened: list[tuple[int, int, str]] = []
    removed: list[str] = []
    receipt_path = _group_receipt_path(state_dir, group)
    before_free = os.statvfs(base).f_bavail * os.statvfs(base).f_frsize
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "PREPARED",
        "prepared_at_utc": _now(),
        "owner": group["owner"],
        "date": group["date"],
        "paths": [str(path) for path in paths],
        "preimages": list(group["preimages"]),
        "source_recovery": group["source_recovery"],
        "allocated_bytes": int(group.get("allocated_bytes") or 0),
        "before_free_bytes": before_free,
        "removed_paths": [],
        "recipe": "recheck every open parent/name binding, unlink one hardlink member, fsync its parent directory",
    }
    try:
        for path, expected in zip(paths, group["preimages"]):
            opened.append(_open_for_apply(path, expected))
        # Every alias must bind to the same inode and remain exactly the planned
        # closed group.  nlink > aliases is an external alias and stays kept.
        first_meta = _metadata(os.fstat(opened[0][0]))
        if first_meta["nlink"] != len(paths):
            raise Keep("inode has an alias outside the known closed group")
        for current, expected in zip(opened, group["preimages"]):
            meta = _metadata(os.fstat(current[0]))
            if meta["dev"] != first_meta["dev"] or meta["ino"] != first_meta["ino"]:
                raise Keep("group members do not share one inode")
        if _snapshot_opened(*opened[0]) != dict(group["preimages"][0]):
            raise Keep("group byte preimage changed before unlink")
        _verify_source_again(group["source_recovery"])
        proc = _proc_census(base, [(first_meta["dev"], first_meta["ino"])], [Path(str(group["namespace"]))])
        if not proc.get("available"):
            raise GateError("complete Linux /proc census is required for apply")
        if proc.get("available") and (proc.get("open_inodes") or proc.get("active_namespaces")):
            raise Keep("target inode or candidate namespace is active")
        _write_json(receipt_path, receipt)
        ctime_after_unlink: int | None = None
        for index, (path, opened_member) in enumerate(zip(paths, opened)):
            fd, parent, name = opened_member
            expected = group["preimages"][index]
            # The group was fully hashed once with every alias open. Content
            # mutation changes ctime; only our previous unlink may change it.
            fd_meta = _metadata(os.fstat(fd))
            if fd_meta["dev"] != expected["dev"] or fd_meta["ino"] != expected["ino"]:
                raise Keep(f"opened inode drifted before unlink: {path}")
            if fd_meta["nlink"] != len(paths) - index:
                raise Keep(f"unexpected hardlink count before unlink: {path}")
            if fd_meta["mode"] != expected["mode"] or fd_meta["uid"] != expected["uid"] or fd_meta["gid"] != expected["gid"] or fd_meta["bytes"] != expected["bytes"] or fd_meta["mtime_ns"] != expected["mtime_ns"]:
                raise Keep(f"target metadata drifted before unlink: {path}")
            if index == 0 and fd_meta["ctime_ns"] != expected["ctime_ns"]:
                raise Keep(f"target ctime drifted before unlink: {path}")
            if index > 0 and ctime_after_unlink is not None and fd_meta["ctime_ns"] != ctime_after_unlink:
                raise Keep(f"target ctime changed outside this collector: {path}")
            named = _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False))
            if not _same_stable(named, fd_meta):
                raise Keep(f"pathname-to-FD binding changed: {path}")
            proc = _proc_census(base, [(fd_meta["dev"], fd_meta["ino"])], [Path(str(group["namespace"]))])
            if not proc.get("available"):
                raise GateError("complete Linux /proc census is required for unlink")
            if proc.get("available") and (proc.get("open_inodes") or proc.get("active_namespaces")):
                raise Keep(f"target became active before unlink: {path}")
            os.unlink(name, dir_fd=parent)
            removed.append(str(path))
            ctime_after_unlink = _metadata(os.fstat(fd))["ctime_ns"]
            os.fsync(parent)
        receipt["status"] = "REMOVED"
        receipt["removed_paths"] = list(removed)
        receipt["actual_allocated_bytes"] = (
            int(group.get("allocated_bytes") or 0)
            if len(removed) == len(paths) else 0
        )
        receipt["removed_at_utc"] = _now()
        receipt["after_free_bytes"] = os.statvfs(base).f_bavail * os.statvfs(base).f_frsize
        receipt["measured_df_netfree_bytes"] = receipt["after_free_bytes"] - before_free
        _write_json(receipt_path, receipt)
        return receipt
    except BaseException as exc:
        receipt["status"] = "PARTIAL" if removed else "KEPT"
        receipt["removed_paths"] = list(removed)
        receipt["actual_allocated_bytes"] = (
            int(group.get("allocated_bytes") or 0)
            if len(removed) == len(paths) else 0
        )
        receipt["error"] = str(exc)
        receipt["after_free_bytes"] = os.statvfs(base).f_bavail * os.statvfs(base).f_frsize
        receipt["measured_df_netfree_bytes"] = receipt["after_free_bytes"] - before_free
        try:
            _write_json(receipt_path, receipt)
        except BaseException:
            pass
        raise RemovalStopped(str(exc), receipt) from exc
    finally:
        for fd, parent, _name in opened:
            try:
                os.close(fd)
            finally:
                os.close(parent)


def _open_for_apply(path: Path, expected: Mapping[str, Any]) -> tuple[int, int, str]:
    parent, name = _safe_open_parent(path)
    fd: int | None = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        meta = _metadata(os.fstat(fd))
        named = _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False))
        if not _same_stable(meta, expected) or not _same_stable(named, meta):
            raise Keep(f"target preimage drifted at apply: {path}")
        return fd, parent, name
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent)
        except OSError:
            pass
        raise


def build_plan(base: str | Path, state_dir: str | Path, *, max_files: int | None = None, max_bytes: int | None = None, runtime: bool = False, apply: bool = False) -> dict[str, Any]:
    """Build a complete dry-run proof, or apply it through the strict seam."""
    base_path = _absolute(base)
    state_path = _absolute(state_dir)
    if max_files is not None and max_files < 0:
        raise ValueError("max_files must be non-negative")
    if apply:
        planned = build_plan(base_path, state_path, max_files=max_files, max_bytes=max_bytes, runtime=runtime, apply=False)
        return apply_plan(planned, base=base_path, state_dir=state_path)
    report: dict[str, Any] = {
        "schema_version": SCHEMA,
        "created_at_utc": _now(),
        "base": str(base_path),
        "state_dir": str(state_path),
        "apply": False,
        "runtime": runtime,
        "groups": [],
        "kept": [],
        "errors": [],
        "unprocessed_by_max_files": 0,
        "complete": True,
    }
    problems = _quiet_problems(base_path, runtime)
    report["quiet_window"] = {"clear": not problems, "problems": list(problems)}
    if problems:
        raise GateError("quiet window blocked: " + "; ".join(problems))
    try:
        refs, refdirs = authority_references(str(base_path))
        private_refs, private_refdirs = _scan_private_runs(base_path)
    except (OSError, ValueError, GateError) as exc:
        raise GateError(f"complete authority scan failed: {exc}") from exc
    refs_set, refdirs_set = _out_reference_sets(base_path, set(refs) | private_refs, set(refdirs) | private_refdirs)
    report["authority"] = {"exact_references": sorted(refs_set), "directory_references": sorted(refdirs_set), "private_runs_scanned": True, "infrastructure_alias_omission": "only venv-main links resolving exactly to base/venv-main are omitted externally; same-root directory/MP4 aliases are validated against one fully scanned canonical target and never traversed"}
    status, pending = candidate_states(str(base_path))
    inflight = in_flight_candidates(str(base_path))
    report["state_gate"] = {"known": len(status), "pending": sorted(pending), "in_flight": sorted(inflight)}
    rows, state_errors = _state_rows(base_path)
    if state_errors:
        raise GateError("complete state scan failed: " + "; ".join(state_errors[:5]))
    aliases, census_errors = _walk_out(base_path)
    if census_errors:
        raise GateError("complete out inode census failed: " + "; ".join(census_errors[:5]))
    out_root = base_path / "out"
    candidates: list[dict[str, Any]] = []
    for inode, paths in sorted(aliases.items(), key=lambda item: sorted(str(path) for path in item[1])):
        selected = [(_candidate_target(path, out_root), path) for path in paths]
        selected = [(target, path) for target, path in selected if target is not None]
        if not selected:
            continue
        target_dates = {target[0] for target, _path in selected}
        target_candidates = {target[1] for target, _path in selected}
        if len(target_dates) != 1 or len(target_candidates) != 1:
            for _target, path in selected:
                report["kept"].append({"path": str(path), "reason": "inode_alias_owner_mismatch"})
            continue
        date = next(iter(target_dates))
        candidate = next(iter(target_candidates))
        target_paths = sorted(paths, key=lambda p: (0 if _candidate_target(p, out_root) and _candidate_target(p, out_root)[2] == 'agy-audio-lrc-input' else 1, str(p)))
        allowed, reason = _group_allowed(target_paths, out_root, candidate, date)
        if not allowed:
            for path in target_paths:
                report["kept"].append({"path": str(path), "reason": reason})
            continue
        owner_rows = rows.get((date, candidate), [])
        # candidate_states() is retained as the global pending/status gate, but
        # its historical setdefault projection can see the same CID on another
        # day.  The owner for this out/<date>/<cid> must be this day's one row.
        if len(owner_rows) != 1 or owner_rows[0][0].get("status") != "candidate_rejected":
            reason = (
                "owner_status_not_candidate_rejected"
                if owner_rows and any(row[0].get("status") for row in owner_rows)
                else "owner_status_unknown"
            )
            report["kept"].append({"paths": [str(path) for path in target_paths], "reason": reason})
            continue
        if candidate in pending:
            report["kept"].append({"paths": [str(path) for path in target_paths], "reason": "owner_has_pending_work"})
            continue
        if candidate in inflight:
            report["kept"].append({"paths": [str(path) for path in target_paths], "reason": "owner_in_flight"})
            continue
        if any(_cited(path, refs_set, refdirs_set) for path in target_paths):
            report["kept"].append({"paths": [str(path) for path in target_paths], "reason": "authority_reference"})
            continue
        if ((max_files is not None and sum(len(g["paths"]) for g in candidates) + len(target_paths) > max_files)
                or (max_bytes is not None and sum(g["allocated_bytes"] for g in candidates) + _lstat(target_paths[0]).st_blocks * 512 > max_bytes)):
            report["kept"].append({"paths": [str(p) for p in target_paths], "reason": "runtime_work_budget"})
            report["unprocessed_by_max_files"] += len(target_paths)
            report["complete"] = False
            continue
        try:
            source_proof = _source_recovery_proof(
                base_path,
                date,
                candidate,
                owner_rows,
                [_candidate_target(path, out_root)[2] for path in target_paths],
                target_paths,
            )
            preimages = _group_preimages(target_paths)
            agy_recipes = source_proof.get('agy_recipes') or []
            if agy_recipes:
                source_hashes = {recipe.get('source_sha256') for recipe in agy_recipes}
                if len(source_hashes) != 1 or next(iter(source_hashes)) != preimages[0]['sha256']:
                    raise Keep('AGY producer input hash differs from exact target inode')
            elif source_proof.get('agy_recipe') and source_proof['agy_recipe']['source_sha256'] != preimages[0]['sha256']:
                # Compatibility for plans produced by the prior singular
                # proof shape; current proofs always carry agy_recipes.
                raise Keep('AGY producer input hash differs from exact target inode')
        except (OSError, Keep, ValueError) as exc:
            report["kept"].append({"paths": [str(path) for path in target_paths], "reason": str(exc)})
            continue
        group = {
            "owner": candidate,
            "date": date,
            "namespace": str(out_root / date / candidate),
            "paths": [str(path) for path in target_paths],
            "categories": [str(_candidate_target(path, out_root)[2]) for path in target_paths],
            "link_class": (
                "source-context-cache"
                if len(target_paths) > 1 and any(_candidate_target(path, out_root)[2] == "context-cache" for path in target_paths)
                else "independent"
            ),
            "inode": {"dev": inode[0], "ino": inode[1], "nlink": len(target_paths)},
            "preimages": preimages,
            "allocated_bytes": preimages[0]["allocated_bytes"],
            "source_recovery": source_proof,
        }
        candidates.append(group)
    candidates.sort(key=lambda group: (str(group["date"]), str(group["owner"]), group["paths"]))
    target_pairs = [(group["inode"]["dev"], group["inode"]["ino"]) for group in candidates]
    proc = _proc_census(base_path, target_pairs, _candidate_namespaces(candidates))
    report["proc_census"] = {
        "available": proc.get("available"),
        "reason": proc.get("reason"),
        "open_inodes": proc.get("open_inodes", []),
        "active_namespaces": proc.get("active_namespaces", []),
    }
    for group in candidates:
        inode = (group["inode"]["dev"], group["inode"]["ino"])
        if proc.get("available") and (any(item.get("dev") == inode[0] and item.get("ino") == inode[1] for item in proc.get("open_inodes", [])) or any(ns.get("cwd") == group["namespace"] or str(group["namespace"]) in ns.get("cmd", "") for ns in proc.get("active_namespaces", []))):
            report["kept"].append({"paths": group["paths"], "reason": "target_or_candidate_namespace_active"})
            continue
        if max_files is not None and sum(len(item["paths"]) for item in report["groups"]) + len(group["paths"]) > max_files:
            report["kept"].append({"paths": group["paths"], "reason": "max_files_limit"})
            report["unprocessed_by_max_files"] += len(group["paths"])
            report["complete"] = False
            continue
        report["groups"].append(group)
    report["planned_files"] = sum(len(group["paths"]) for group in report["groups"])
    report["planned_allocated_bytes"] = sum(int(group["allocated_bytes"]) for group in report["groups"])
    report["target_inode_census"] = {"regular_inodes": len(aliases), "candidate_groups": len(candidates)}
    return report


def apply_plan(report: Mapping[str, Any], *, base: str | Path | None = None, state_dir: str | Path | None = None) -> dict[str, Any]:
    """Apply a previously written dry-run report, stopping at the first group.

    This public seam acquires ``runner.lock`` while repeating the exact proof.
    It is useful for an operator that wants to inspect the dry-run before a
    separate apply invocation; each group still rechecks every preimage and
    source edge probe before unlinking.
    """
    if report.get("schema_version") != SCHEMA or report.get("apply"):
        raise GateError("only a dry-run terminal-out plan can be applied")
    base_path = _absolute(base or str(report.get("base") or ""))
    state_path = _absolute(state_dir or str(report.get("state_dir") or ""))
    lock_fd = None
    try:
        lock_fd, _ = _runner_lock(base_path)
        groups = report.get("groups")
        if not isinstance(groups, list):
            raise GateError("plan groups are missing")
        result = dict(report)
        result["apply"] = True
        result["removed"] = []
        result["partial"] = []
        # Re-run host gates and source/reference state before consuming the
        # saved proof.  A changed namespace must force a fresh dry-run.
        if (problems := _quiet_problems(base_path, bool(report.get("runtime")))):
            raise GateError("quiet window blocked: " + "; ".join(problems))
        tier_refs, tier_dirs = authority_references(str(base_path))
        private_refs, private_dirs = _scan_private_runs(base_path)
        current_refs, current_dirs = _out_reference_sets(
            base_path, set(tier_refs) | private_refs, set(tier_dirs) | private_dirs
        )
        old_refs = set((report.get("authority") or {}).get("exact_references") or [])
        old_dirs = set((report.get("authority") or {}).get("directory_references") or [])
        if current_refs != old_refs or current_dirs != old_dirs:
            raise GateError("authority references changed since planning")
        status, pending = candidate_states(str(base_path))
        inflight = in_flight_candidates(str(base_path))
        current_rows, state_errors = _state_rows(base_path)
        if state_errors:
            raise GateError("complete state scan failed before apply: " + "; ".join(state_errors[:5]))
        for group in groups:
            owner = str(group.get("owner") or "")
            date = str(group.get("date") or "")
            owner_rows = current_rows.get((date, owner), [])
            if len(owner_rows) != 1 or owner_rows[0][0].get("status") != "candidate_rejected":
                raise GateError(f"owner state changed before apply: {owner}")
            if owner in pending or owner in inflight:
                raise GateError(f"owner became pending/in-flight before apply: {owner}")
            categories = group.get("categories")
            if not isinstance(categories, list):
                raise GateError(f"target categories missing before apply: {owner}")
            current_proof = _source_recovery_proof(
                base_path, date, owner, owner_rows, [str(category) for category in categories],
                [Path(p) for p in group['paths']]
            )
            if current_proof != group.get("source_recovery"):
                raise GateError(f"source recovery proof changed before apply: {owner}")
        pass_before_free = os.statvfs(base_path).f_bavail * os.statvfs(base_path).f_frsize
        for group in groups:
            try:
                receipt = _apply_group(group, base_path, state_path)
            except BaseException as exc:
                partial: dict[str, Any] = {"paths": group.get("paths", []), "reason": str(exc)}
                if isinstance(exc, RemovalStopped):
                    partial.update(exc.receipt)
                else:
                    try:
                        partial.update(json.loads(_group_receipt_path(state_path, group).read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        pass
                result["partial"].append(partial)
                result["complete"] = False
                break
            result["removed"].append(receipt)
        all_receipts = [*result.get("removed", []), *result.get("partial", [])]
        result["removed_files"] = sum(len(item.get("removed_paths", [])) for item in all_receipts)
        result["removed_allocated_bytes"] = sum(int(item.get("actual_allocated_bytes") or 0) for item in all_receipts)
        # _apply_group has closed its proof FDs before returning. Measuring the
        # whole pass here includes blocks freed only when the last FD closes.
        result["before_free_bytes"] = pass_before_free
        result["after_free_bytes"] = os.statvfs(base_path).f_bavail * os.statvfs(base_path).f_frsize
        result["removed_df_netfree_bytes"] = result["after_free_bytes"] - pass_before_free
        return result
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _runner_lock(base: Path) -> tuple[int, bool]:
    path = base / "runner.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise GateError("runner.lock is a symlink")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise GateError("runner.lock is held by another process") from exc
    return fd, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/opt/bilive/autoslice"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/autoslice-runtime-gc"))
    parser.add_argument("--apply", action="store_true", help="delete only after the complete repeated proof")
    parser.add_argument("--runtime", action="store_true", help="between ticks: apply uses the real runner mutex instead of requiring DISABLED")
    parser.add_argument("--max-bytes", type=int, default=None, help="optional allocated-byte work budget")
    parser.add_argument("--max-files", type=int, default=None, help="optional file-count work bound; default processes the full set")
    parser.add_argument("--json", type=Path, default=None, help="plan/report path; defaults to state-dir/terminal-out-gc-plan.json")
    args = parser.parse_args(argv)
    if args.max_bytes is not None and args.max_bytes < 0:
        parser.error("--max-bytes must be non-negative")
    if args.max_files is not None and args.max_files < 0:
        parser.error("--max-files must be non-negative")
    base = _absolute(args.base)
    state_dir = _absolute(args.state_dir)
    output = _absolute(args.json) if args.json else state_dir / "terminal-out-gc-plan.json"
    def stop_at_budget(_signum, _frame):
        raise TimeoutError('runtime GC terminated at its wall-clock budget')
    signal.signal(signal.SIGTERM, stop_at_budget)
    try:
        if args.apply:
            # Avoid reading gigabytes when the runner is already busy. This
            # probe is only a work-saving check; apply_plan reacquires the lock
            # and repeats every gate, so it does not grant later deletion.
            probe_fd, _ = _runner_lock(base)
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
            os.close(probe_fd)
            # Plan first, then let apply_plan reacquire runner.lock and repeat
            # every authority/state gate immediately before unlinking.  This
            # keeps a direct CLI --apply as strict as the inspect-then-apply
            # seam instead of treating one scan as a durable authorization.
            planned = build_plan(base, state_dir, max_files=args.max_files, max_bytes=args.max_bytes, runtime=args.runtime, apply=False)
            report = apply_plan(planned, base=base, state_dir=state_dir)
        else:
            report = build_plan(base, state_dir, max_files=args.max_files, max_bytes=args.max_bytes, runtime=args.runtime, apply=False)
        _write_json(output, report)
        print(json.dumps({"status": "COMPLETE" if report.get("complete") else "PARTIAL", "plan": str(output), "planned_files": report.get("planned_files", 0), "removed_files": report.get("removed_files", 0)}, ensure_ascii=False))
        return 0 if report.get("complete") else 2
    except (GateError, OSError, Keep, ValueError) as exc:
        error_report = {"schema_version": SCHEMA, "created_at_utc": _now(), "base": str(base), "state_dir": str(state_dir), "apply": bool(args.apply), "complete": False, "errors": [str(exc)], "groups": [], "kept": []}
        try:
            _write_json(output, error_report)
        except BaseException:
            pass
        print(f"terminal-out-gc BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
