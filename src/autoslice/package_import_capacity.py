"""Destination-capacity preflight for external package copies."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.failed_pick_import import PackageImportError
from src.autoslice.runner_batch_dispatch import _parse_min_free_bytes
from src.autoslice import runtime_storage

_PLANNED_WRITE_STATUSES = frozenset({"WOULD_COPY", "WOULD_OVERWRITE_DIVERGENT"})


def copy_capacity(
    entries: Sequence[Mapping[str, Any]],
    *,
    runtime_base: Path | None = None,
    operational_reserve_bytes: int | None = None,
    pressure_threshold_bytes: int | None = None,
    free_bytes_fn: Any | None = None,
) -> list[dict[str, Any]]:
    """Require exact planned allocations plus the operational reserve.

    This is a preflight, not a reservation. Existing identical or protected files
    consume no planned bytes; an atomic replacement consumes the full new payload.
    ``AUTOSLICE_MIN_FREE_BYTES`` is only a pressure threshold.  The operational
    reserve is independently configured and defaults to 2 GiB.
    """

    try:
        if pressure_threshold_bytes is None:
            threshold = _parse_min_free_bytes()
        elif (
            isinstance(pressure_threshold_bytes, bool)
            or not isinstance(pressure_threshold_bytes, int)
            or pressure_threshold_bytes < 0
        ):
            raise ValueError("AUTOSLICE_MIN_FREE_BYTES must be a non-negative integer")
        else:
            threshold = pressure_threshold_bytes
        effective_base = runtime_base
        if effective_base is None and os.environ.get("AUTOSLICE_BASE"):
            effective_base = Path(os.environ["AUTOSLICE_BASE"])
        if operational_reserve_bytes is not None:
            reserve = runtime_storage.parse_operational_reserve_bytes(
                operational_reserve_bytes
            )
        elif os.environ.get(runtime_storage.OPERATIONAL_RESERVE_ENV) is not None:
            reserve = runtime_storage.parse_operational_reserve_bytes()
        else:
            reserve = runtime_storage.parse_operational_reserve_bytes()
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
                    "operational_reserve_bytes": reserve,
                    "pressure_threshold_bytes": threshold,
                },
            )
            size = entry.get("bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("planned copy bytes must be a non-negative integer")
            volume["planned_copy_bytes"] += size

        for volume in volumes.values():
            probe = free_bytes_fn or (lambda path: shutil.disk_usage(path).free)
            available_before = probe(volume["path"])
            if isinstance(available_before, bool) or not isinstance(available_before, int) or available_before < 0:
                raise ValueError("disk free-space probe returned an invalid integer")
            reclaimed = 0
            pressure = bool(threshold and available_before < threshold)
            if pressure:
                if effective_base is not None:
                    try:
                        gc_result = runtime_storage.collect_ephemeral(
                            Path(effective_base), reason="package-import-pressure"
                        )
                        if isinstance(gc_result, Mapping):
                            reclaimed = int(gc_result.get("reclaimed_bytes", 0))
                    except runtime_storage.RuntimeStorageError as error:
                        volume["gc_error"] = str(error)
                try:
                    runtime_storage.emit_pressure_request(
                        Path(effective_base) if effective_base is not None else None,
                        available_bytes=available_before,
                        pressure_threshold_bytes=threshold,
                        required_bytes=volume["planned_copy_bytes"] + reserve,
                        planned_copy_bytes=volume["planned_copy_bytes"],
                        candidate_id=next(
                            (
                                str(row.get("candidate_id") or row.get("cid"))
                                for row in entries
                                if row.get("candidate_id") or row.get("cid")
                            ),
                            None,
                        ),
                        reason="package-import-below-pressure-threshold",
                        reclaimed_bytes=reclaimed,
                    )
                except runtime_storage.RuntimeStorageError as error:
                    raise OSError(str(error)) from error
            volume["available_before_gc_bytes"] = available_before
            volume["reclaimed_bytes"] = reclaimed
            # Probe again after live GC.  This keeps the exact package
            # admission decision tied to the post-cleanup measurement while
            # retaining the single-probe behavior for ordinary imports.
            available = probe(volume["path"]) if pressure and effective_base is not None else available_before
            if isinstance(available, bool) or not isinstance(available, int) or available < 0:
                raise ValueError("disk free-space probe returned an invalid integer")
            volume["available_bytes"] = available
            volume["pressure_triggered"] = pressure
            volume["required_bytes"] = volume["planned_copy_bytes"] + reserve
            if available < volume["required_bytes"]:
                raise PackageImportError(
                    "INSUFFICIENT_DISK_SPACE",
                    f"{volume['path']}: available={available} "
                    f"required={volume['required_bytes']} "
                    f"(copy={volume['planned_copy_bytes']} reserve={reserve})",
                    hint="restore authorized destination capacity before importing; "
                    "this check does not authorize cleanup or lowering the reserve",
                )
    except PackageImportError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise PackageImportError("DISK_CAPACITY_UNAVAILABLE", str(error)) from error
    return list(volumes.values())
