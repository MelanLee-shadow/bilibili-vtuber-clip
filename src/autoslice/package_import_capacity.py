"""Destination-capacity preflight for external package copies."""

from __future__ import annotations

import errno
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.failed_pick_import import PackageImportError
from src.autoslice.runner_batch_dispatch import _parse_min_free_bytes

_PLANNED_WRITE_STATUSES = frozenset({"WOULD_COPY", "WOULD_OVERWRITE_DIVERGENT"})


def copy_capacity(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Require planned allocations plus the configured reserve per filesystem.

    This is a preflight, not a reservation. Existing identical or protected files
    consume no planned bytes; an atomic replacement consumes the full new payload.
    """

    try:
        reserve = _parse_min_free_bytes()
    except ValueError as error:
        raise PackageImportError("DISK_CAPACITY_POLICY_INVALID", str(error)) from error

    volumes: dict[int, dict[str, Any]] = {}
    try:
        for entry in entries:
            if entry["status"] not in _PLANNED_WRITE_STATUSES:
                continue
            ancestor = Path(entry["destination"]).parent
            while True:
                try:
                    ancestor_stat = ancestor.stat()
                except FileNotFoundError:
                    # Only an actually missing path may fall back to its parent.
                    # exists() can hide permission/I/O errors and select the
                    # wrong filesystem. Stop if even the root cannot be found.
                    if ancestor.parent == ancestor:
                        raise
                    ancestor = ancestor.parent
                else:
                    break
            if not stat.S_ISDIR(ancestor_stat.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "capacity ancestor is not a directory", str(ancestor))
            device = ancestor_stat.st_dev
            volume = volumes.setdefault(
                device,
                {
                    "path": str(ancestor),
                    "device": device,
                    "planned_copy_bytes": 0,
                    "reserve_bytes": reserve,
                },
            )
            volume["planned_copy_bytes"] += int(entry["bytes"])

        for volume in volumes.values():
            volume["available_bytes"] = shutil.disk_usage(volume["path"]).free
            volume["required_bytes"] = volume["planned_copy_bytes"] + reserve
            if volume["available_bytes"] < volume["required_bytes"]:
                raise PackageImportError(
                    "INSUFFICIENT_DISK_SPACE",
                    f"{volume['path']}: available={volume['available_bytes']} "
                    f"required={volume['required_bytes']} "
                    f"(copy={volume['planned_copy_bytes']} reserve={reserve})",
                    hint="restore authorized destination capacity before importing; "
                    "this check does not authorize cleanup or lowering the reserve",
                )
    except OSError as error:
        raise PackageImportError("DISK_CAPACITY_UNAVAILABLE", str(error)) from error
    return list(volumes.values())
