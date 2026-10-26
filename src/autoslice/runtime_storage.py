"""Capacity admission and deterministic cleanup of owned runtime scratch."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import uuid


GIB = 1024**3
DEFAULT_OPERATIONAL_RESERVE_BYTES = 2 * GIB
DEFAULT_PEAK_BYTES_PER_SECOND = 16 * 1024**2
OPERATIONAL_RESERVE_ENV = "AUTOSLICE_OPERATIONAL_RESERVE_BYTES"
PEAK_BYTES_PER_SECOND_ENV = "AUTOSLICE_CAPACITY_PEAK_BYTES_PER_SECOND"
PRESSURE_REQUEST_SCHEMA = "autoslice-capacity-pressure-request.v1"
SCRATCH_REGISTRY_SCHEMA = "autoslice-runtime-scratch-registry.v1"
PRESSURE_REQUEST_RELATIVE_PATH = Path("reports/capacity-pressure-requests.v1.json")
SCRATCH_ROOT_NAME = "runtime-scratch"
SCRATCH_REGISTRY_NAME = "registry.v1.json"
_LOCK = threading.RLock()
_REQUEST_LOCK = threading.RLock()


class RuntimeStorageError(RuntimeError):
    """A scratch or durable runtime document could not be proven safe."""


class CapacityBudgetError(ValueError):
    """A conservative allocation budget is missing or invalid."""


class CapacityProbeError(RuntimeError):
    """The free-space probe failed or returned an invalid value."""


def _parse_bytes(raw: object, *, name: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or re.fullmatch(r"[0-9]+", str(raw).strip()) is None:
        raise ValueError(f"{name} must be a non-negative integer")
    return int(str(raw).strip())


def parse_operational_reserve_bytes(raw: object | None = None) -> int:
    return _parse_bytes(
        os.environ.get(OPERATIONAL_RESERVE_ENV) if raw is None else raw,
        name=OPERATIONAL_RESERVE_ENV,
        default=DEFAULT_OPERATIONAL_RESERVE_BYTES,
    )


def parse_peak_bytes_per_second(raw: object | None = None) -> int:
    return _parse_bytes(
        os.environ.get(PEAK_BYTES_PER_SECOND_ENV) if raw is None else raw,
        name=PEAK_BYTES_PER_SECOND_ENV,
        default=DEFAULT_PEAK_BYTES_PER_SECOND,
    )


def _int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise CapacityBudgetError(f"{label} must be a non-negative integer")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    raise CapacityBudgetError(f"{label} must be a non-negative integer")


def _field(item: Mapping[str, object], names: Sequence[str], *, label: str) -> int | None:
    values = [_int(item[name], label=f"{label}.{name}") for name in names if name in item]
    if not values:
        return None
    if len(set(values)) != 1:
        raise CapacityBudgetError(f"conflicting {label} fields")
    return values[0]


def _duration_ms(item: Mapping[str, object]) -> int | None:
    # Actual clip bounds win over source/anchor bounds and whole-recording
    # duration.  This avoids reserving an hour for a one-minute clip.
    explicit_capacity = _field(item, ("capacity_duration_ms",), label="capacity_duration_ms")
    if explicit_capacity is not None:
        return explicit_capacity
    for start, end in (
        ("clip_start_ms", "clip_end_ms"),
        ("start_ms", "end_ms"),
        ("window_start_ms", "window_end_ms"),
        ("source_start_ms", "source_end_ms"),
        ("anchor_start_ms", "anchor_end_ms"),
    ):
        if start not in item and end not in item:
            continue
        if start not in item or end not in item:
            raise CapacityBudgetError(f"incomplete duration pair {start}/{end}")
        begin, finish = _int(item[start], label=start), _int(item[end], label=end)
        if finish < begin:
            raise CapacityBudgetError(f"{end} precedes {start}")
        return finish - begin
    duration = _field(
        item,
        ("capacity_duration_ms", "window_duration_ms", "planned_duration_ms", "duration_ms", "seg_dur_ms"),
        label="duration_ms",
    )
    seconds = _field(item, ("duration_seconds", "duration_s", "window_seconds"), label="duration_seconds")
    if seconds is not None:
        duration = seconds * 1000 if duration is None else duration
    if "duration" in item:
        try:
            value = float(item["duration"])
        except (TypeError, ValueError) as exc:
            raise CapacityBudgetError("duration must be numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise CapacityBudgetError("duration must be a finite non-negative number")
        duration = math.ceil(value * 1000) if duration is None else duration
    return duration


def _planned_source_bytes(item: Mapping[str, object]) -> int:
    """Count source bytes only when the caller declares a planned source copy.

    ``segment_path``/``source_path`` identify read-only media consumed by the
    talk and song lanes.  Stat'ing those files here would reserve an entire
    recording for a bounded clip.  A lane that really copies a source must
    provide an explicit byte count or ``planned_source_paths``/``source_copy_paths``.
    """

    explicit = _field(
        item,
        ("planned_source_bytes", "source_copy_bytes"),
        label="source_bytes",
    )
    values: list[object] = []
    for name in ("planned_source_paths", "source_copy_paths"):
        raw = item.get(name)
        if raw is None:
            continue
        if isinstance(raw, (str, os.PathLike)):
            values.append(raw)
        elif isinstance(raw, Sequence):
            values.extend(raw)
        else:
            raise CapacityBudgetError(f"{name} must be a path or sequence of paths")
    for name in ("planned_source_pieces", "source_copy_pieces"):
        raw = item.get(name)
        if raw is None:
            continue
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise CapacityBudgetError(f"{name} must be a sequence of source pieces")
        for piece in raw:
            if isinstance(piece, (str, os.PathLike)):
                values.append(piece)
            elif isinstance(piece, Mapping):
                values.extend(
                    piece[key]
                    for key in ("segment_path", "source_path", "source_media_path", "remote_media", "path")
                    if key in piece and piece[key] is not None
                )
            else:
                raise CapacityBudgetError(f"{name} contains an invalid source piece")
    total, seen = explicit or 0, set()
    for raw in values:
        if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
            raise CapacityBudgetError("source path is invalid; source copy budget is unknown")
        path = Path(os.fspath(raw))
        key = os.path.abspath(os.fspath(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise CapacityBudgetError(
                f"source size is unknown because it is not materialized: {path}"
            ) from exc
        except OSError as exc:
            raise CapacityBudgetError(f"source size probe failed for {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise CapacityBudgetError(f"source path is not a regular file: {path}")
        total += int(info.st_size)
    return total


@dataclass(frozen=True)
class CapacityBudget:
    estimated_peak_bytes: int
    duration_ms: int | None
    peak_bytes_per_second: int
    known_planned_copy_bytes: int
    known_source_bytes: int
    explicit_peak_bytes: int

    def as_dict(self) -> dict[str, int | None]:
        return self.__dict__.copy()


def estimate_peak_allocation(
    item: Mapping[str, object], *, peak_bytes_per_second: int = DEFAULT_PEAK_BYTES_PER_SECOND
) -> CapacityBudget:
    rate = _int(peak_bytes_per_second, label="peak_bytes_per_second")
    duration = _duration_ms(item)
    planned = _field(
        item,
        ("planned_copy_bytes", "known_planned_copy_bytes", "planned_bytes", "copy_bytes", "package_bytes", "expected_output_bytes"),
        label="planned_copy_bytes",
    ) or 0
    source = _planned_source_bytes(item)
    explicit = _field(
        item, ("capacity_peak_bytes", "peak_new_bytes", "estimated_peak_bytes"), label="capacity_peak_bytes"
    ) or 0
    if duration is None and planned == 0 and source == 0 and explicit == 0:
        raise CapacityBudgetError("capacity budget is unknown")
    duration_bytes = 0 if duration is None else ((duration + 999) // 1000) * rate
    return CapacityBudget(duration_bytes + planned + source + explicit, duration, rate, planned, source, explicit)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _document_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_object(path: Path, default: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeStorageError(f"invalid runtime document: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeStorageError(f"runtime document is not an object: {path}")
    return value


def emit_pressure_request(
    runtime_base: Path | None,
    *,
    available_bytes: int,
    pressure_threshold_bytes: int,
    required_bytes: int | None = None,
    estimated_peak_bytes: int | None = None,
    planned_copy_bytes: int | None = None,
    candidate_id: str | None = None,
    date: str | None = None,
    reason: str = "below-pressure-threshold",
    reclaimed_bytes: int = 0,
) -> Path | None:
    if runtime_base is None:
        return None
    key = hashlib.sha256(
        _json_bytes({
            "candidate_id": candidate_id,
            "date": date,
            "estimated_peak_bytes": estimated_peak_bytes,
            "planned_copy_bytes": planned_copy_bytes,
            "pressure_threshold_bytes": pressure_threshold_bytes,
            "reason": reason,
        })
    ).hexdigest()
    path = Path(runtime_base) / PRESSURE_REQUEST_RELATIVE_PATH
    with _REQUEST_LOCK, _document_lock(path):
        document = _read_object(path, {"schema_version": PRESSURE_REQUEST_SCHEMA, "requests": []})
        if document.get("schema_version") not in (None, PRESSURE_REQUEST_SCHEMA):
            raise RuntimeStorageError(f"pressure request schema drift: {path}")
        rows = document.get("requests")
        if not isinstance(rows, list):
            raise RuntimeStorageError(f"pressure request list invalid: {path}")
        if any(isinstance(row, Mapping) and row.get("request_key") == key for row in rows):
            return path
        row = {
            "request_key": key,
            "requested_at": _now(),
            "candidate_id": candidate_id,
            "date": date,
            "reason": reason,
            "available_bytes": available_bytes,
            "pressure_threshold_bytes": pressure_threshold_bytes,
            "required_bytes": required_bytes,
            "estimated_peak_bytes": estimated_peak_bytes,
            "planned_copy_bytes": planned_copy_bytes,
            "reclaimed_bytes": reclaimed_bytes,
            "native_housekeeping_session": {"requested": True, "created": False},
        }
        rows.append(row)
        document.update({"schema_version": PRESSURE_REQUEST_SCHEMA, "requests": rows, "latest": row})
        _atomic_json(path, document)
    return path


def _scratch_root(base: Path) -> Path:
    return Path(os.path.abspath(os.fspath(base))) / SCRATCH_ROOT_NAME


def _registry_path(base: Path) -> Path:
    return _scratch_root(base) / SCRATCH_REGISTRY_NAME


def _load_registry(base: Path) -> dict[str, object]:
    path = _registry_path(base)
    document = _read_object(path, {"schema_version": SCRATCH_REGISTRY_SCHEMA, "entries": []})
    if document.get("schema_version") not in (None, SCRATCH_REGISTRY_SCHEMA):
        raise RuntimeStorageError(f"scratch registry schema drift: {path}")
    if not isinstance(document.get("entries"), list):
        raise RuntimeStorageError(f"scratch registry entries invalid: {path}")
    document["schema_version"] = SCRATCH_REGISTRY_SCHEMA
    return document


def _owned_workspace(base: Path, path: Path) -> bool:
    root = _scratch_root(base)
    if root.is_symlink() or not root.is_dir():
        return False
    target = Path(os.path.abspath(os.fspath(path)))
    return target.parent == root and target != root and target.name.startswith("work-")


@dataclass
class ScratchLease:
    base: Path
    path: Path
    owner: str
    state: str = "active"

    def release(self, *, failed: bool = False) -> None:
        if self.state != "active":
            return
        state = "failed" if failed else "released"
        _set_scratch_state(self.base, self.path, state, self.owner)
        self.state = state

    def __enter__(self) -> "ScratchLease":
        return self

    def __exit__(self, exc_type: object, _value: object, _traceback: object) -> None:
        self.release(failed=exc_type is not None)


def create_ephemeral_workspace(runtime_base: Path, *, owner: str) -> ScratchLease:
    base = Path(os.path.abspath(os.fspath(runtime_base)))
    root = _scratch_root(base)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeStorageError(f"runtime scratch root is unsafe: {root}")
    path = root / f"work-{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    try:
        with _LOCK, _document_lock(_registry_path(base)):
            document = _load_registry(base)
            document["entries"].append({"path": str(path), "owner": owner, "state": "active", "created_at": _now()})
            _atomic_json(_registry_path(base), document)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return ScratchLease(base, path, owner)


def _set_scratch_state(base: Path, path: Path, state: str, owner: str) -> None:
    with _LOCK, _document_lock(_registry_path(base)):
        document = _load_registry(base)
        for row in document["entries"]:
            if isinstance(row, dict) and row.get("path") == str(path):
                if row.get("owner") != owner:
                    raise RuntimeStorageError(f"scratch owner mismatch: {path}")
                row.update({"state": state, "released_at": _now()})
                _atomic_json(_registry_path(base), document)
                return
    raise RuntimeStorageError(f"scratch lease not found: {path}")


def _tree_bytes(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    if not path.is_dir() or path.is_symlink():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += int(child.stat().st_size)
        except OSError:
            continue
    return total


def collect_ephemeral(runtime_base: Path, *, reason: str = "runtime-pressure") -> dict[str, object]:
    """Delete only released, directly registered workspaces."""

    base = Path(os.path.abspath(os.fspath(runtime_base)))
    removed, skipped, reclaimed = [], [], 0
    with _LOCK, _document_lock(_registry_path(base)):
        document = _load_registry(base)
        retained: list[object] = []
        for raw in document["entries"]:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("path"), str):
                retained.append(raw)
                skipped.append({"path": "<invalid>", "reason": "invalid-entry"})
                continue
            path = Path(os.path.abspath(raw["path"]))
            if raw.get("state") != "released":
                retained.append(raw)
                skipped.append({"path": str(path), "reason": str(raw.get("state") or "unknown")})
                continue
            if not _owned_workspace(base, path) or path.is_symlink():
                retained.append(raw)
                skipped.append({"path": str(path), "reason": "outside-owned-root"})
                continue
            try:
                logical_bytes = _tree_bytes(path)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                reclaimed += logical_bytes
                removed.append(str(path))
            except OSError as exc:
                retained.append(raw)
                skipped.append({"path": str(path), "reason": str(exc)})
        if len(retained) != len(document["entries"]):
            document["entries"] = retained
            _atomic_json(_registry_path(base), document)
    return {
        "schema_version": SCRATCH_REGISTRY_SCHEMA,
        "reason": reason,
        "reclaimed_bytes": reclaimed,
        "logical_removed_bytes": reclaimed,
        "removed": removed,
        "skipped": skipped,
    }


@dataclass
class Reservation:
    controller: "CapacityAdmission"
    bytes: int
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.controller.release(self)
            self.released = True


class CapacityAdmission:
    """Keep this batch's reservations from oversubscribing fresh free space."""

    def __init__(
        self,
        *,
        runtime_base: Path | None,
        free_bytes_fn: Callable[[Path], int],
        operational_reserve_bytes: int,
        pressure_threshold_bytes: int = 0,
        peak_bytes_per_second: int = DEFAULT_PEAK_BYTES_PER_SECOND,
        gc_fn: Callable[[Path], Mapping[str, object]] | None = None,
        log: Callable[[str], None] | None = None,
        date: str | None = None,
    ) -> None:
        self.runtime_base = runtime_base
        self.free_bytes_fn = free_bytes_fn
        self.operational_reserve_bytes = _int(operational_reserve_bytes, label="operational_reserve_bytes")
        self.pressure_threshold_bytes = _int(pressure_threshold_bytes, label="pressure_threshold_bytes")
        self.peak_bytes_per_second = _int(peak_bytes_per_second, label="peak_bytes_per_second")
        self.gc_fn = gc_fn or (lambda base: collect_ephemeral(base, reason="before-admission"))
        self.log = log or (lambda _message: None)
        self.date = date
        self._reserved = 0
        self._lock = threading.RLock()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved

    def _probe(self, base: Path) -> int:
        try:
            value = self.free_bytes_fn(base)
        except Exception as exc:  # noqa: BLE001
            raise CapacityProbeError(f"disk free-space probe failed: {exc}") from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CapacityProbeError("disk free-space probe returned an invalid integer")
        return value

    def reserve_for_item(
        self,
        item: Mapping[str, object],
        *,
        candidate_id: str | None = None,
        date: str | None = None,
    ) -> tuple[Reservation | None, dict[str, object]]:
        budget = estimate_peak_allocation(item, peak_bytes_per_second=self.peak_bytes_per_second)
        base = Path(self.runtime_base or ".")
        with self._lock:
            available = self._probe(base)
            reclaimed = 0
            pressure = bool(self.pressure_threshold_bytes and available < self.pressure_threshold_bytes)
            if pressure:
                initial = available
                try:
                    result = self.gc_fn(base)
                    if isinstance(result, Mapping):
                        reclaimed = int(result.get("reclaimed_bytes", 0))
                except Exception as exc:  # noqa: BLE001
                    self.log(f"runtime scratch GC failed under pressure: {exc}")
                available = self._probe(base)
                emit_pressure_request(
                    self.runtime_base,
                    available_bytes=initial,
                    pressure_threshold_bytes=self.pressure_threshold_bytes,
                    required_bytes=self.operational_reserve_bytes + budget.estimated_peak_bytes + self._reserved,
                    estimated_peak_bytes=budget.estimated_peak_bytes,
                    planned_copy_bytes=budget.known_planned_copy_bytes,
                    candidate_id=candidate_id,
                    date=date or self.date,
                    reclaimed_bytes=reclaimed,
                )
            required = self.operational_reserve_bytes + budget.estimated_peak_bytes + self._reserved
            details: dict[str, object] = {
                "available_bytes": available,
                "required_bytes": required,
                "operational_reserve_bytes": self.operational_reserve_bytes,
                "in_flight_reserved_bytes": self._reserved,
                "pressure_triggered": pressure,
                "budget": budget.as_dict(),
            }
            if available < required:
                details["reason"] = "insufficient-peak-capacity"
                if pressure:
                    emit_pressure_request(
                        self.runtime_base,
                        available_bytes=available,
                        pressure_threshold_bytes=self.pressure_threshold_bytes,
                        required_bytes=required,
                        estimated_peak_bytes=budget.estimated_peak_bytes,
                        planned_copy_bytes=budget.known_planned_copy_bytes,
                        candidate_id=candidate_id,
                        date=date or self.date,
                        reason="insufficient-peak-capacity",
                        reclaimed_bytes=reclaimed,
                    )
                return None, details
            self._reserved += budget.estimated_peak_bytes
            return Reservation(self, budget.estimated_peak_bytes), {**details, "admitted": True}

    def reserve_bytes(self, amount: int) -> Reservation | None:
        amount = _int(amount, label="reservation_bytes")
        with self._lock:
            available = self._probe(Path(self.runtime_base or "."))
            if available < self.operational_reserve_bytes + amount + self._reserved:
                return None
            self._reserved += amount
            return Reservation(self, amount)

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - reservation.bytes)


__all__ = [
    "CapacityAdmission",
    "CapacityBudget",
    "CapacityBudgetError",
    "CapacityProbeError",
    "DEFAULT_OPERATIONAL_RESERVE_BYTES",
    "DEFAULT_PEAK_BYTES_PER_SECOND",
    "OPERATIONAL_RESERVE_ENV",
    "PEAK_BYTES_PER_SECOND_ENV",
    "Reservation",
    "RuntimeStorageError",
    "ScratchLease",
    "collect_ephemeral",
    "create_ephemeral_workspace",
    "emit_pressure_request",
    "estimate_peak_allocation",
    "parse_operational_reserve_bytes",
    "parse_peak_bytes_per_second",
]
