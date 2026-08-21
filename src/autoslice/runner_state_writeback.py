"""Conflict-safe writeback for long-lived free-runner state snapshots.

Atomic rename prevents torn JSON, but it does not prevent a long runner tick
from replacing legal out-of-band candidate updates with its stale in-memory
document.  A state returned by :func:`track_state` therefore retains the last
disk snapshot it observed.  Each later write rereads the current document and
performs a three-way merge.  Candidate rows are compared across all queue and
result collections so a local move (pending -> picks) cannot duplicate a row
whose same candidate was changed externally.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Callable, Mapping, Sequence


CANDIDATE_COLLECTIONS = (
    "pending_talk",
    "pending_song",
    "picks",
    "songs",
    "talk_backlog",
    "song_backlog",
    "song_selection_backlog",
    "talk_below_confidence_threshold",
    "talk_superseded_attempts",
    "song_superseded_attempts",
    "talk_selection_overrides",
    "talk_user_suppressions",
    "recovery_requeue_deferrals",
    "song_quarantine_intervals",
    "song_edge_bgm_excluded",
    "song_overlap_blocked_talk",
)
MAX_CAS_ATTEMPTS = 5
_MISSING = object()


class RunnerStateWritebackError(RuntimeError):
    """The disk authority could not be safely merged or replaced."""


class TrackedState(dict):
    """A normal state dict carrying one private, non-serialized base snapshot."""

    def __init__(self, path: Path, value: Mapping[str, object]) -> None:
        super().__init__(deepcopy(dict(value)))
        self._writeback_path = Path(path)
        self._writeback_base = deepcopy(dict(value))

    def accept_writeback(self, value: Mapping[str, object]) -> None:
        self.clear()
        self.update(deepcopy(dict(value)))
        self._writeback_base = deepcopy(dict(value))


def track_state(path: Path, state: Mapping[str, object]) -> TrackedState:
    """Bind a freshly read state document to the bytes it may later replace."""

    if isinstance(state, TrackedState) and state._writeback_path == Path(path):
        return state
    return TrackedState(path, state)


def make_date_state_writer(
    path_for_date: Callable[[str], Path],
    *,
    updated_at: Callable[[], str],
    log: Callable[[str], None],
) -> Callable[[str, dict], None]:
    """Bind runner clock/path dependencies without duplicating a wrapper."""

    def write(date: str, state: dict) -> None:
        write_state(path_for_date(date), state, updated_at=updated_at(), log=log)

    return write


def _same(left: object, right: object) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return left == right


def _merge_value(
    base: object,
    disk: object,
    local: object,
    *,
    path: str,
    conflicts: list[str],
) -> object:
    if _same(local, base):
        return deepcopy(disk) if disk is not _MISSING else _MISSING
    if _same(disk, base) or _same(local, disk):
        return deepcopy(local) if local is not _MISSING else _MISSING
    if (
        isinstance(disk, Mapping)
        and isinstance(local, Mapping)
        and (base is _MISSING or isinstance(base, Mapping))
    ):
        return _merge_mapping(
            {} if base is _MISSING else base,
            disk,
            local,
            path=path,
            conflicts=conflicts,
        )
    conflicts.append(path)
    return deepcopy(disk) if disk is not _MISSING else _MISSING


def _merge_mapping(
    base: Mapping[str, object],
    disk: Mapping[str, object],
    local: Mapping[str, object],
    *,
    path: str,
    conflicts: list[str],
) -> dict[str, object]:
    merged: dict[str, object] = {}
    for key in sorted(set(base) | set(disk) | set(local)):
        child = _merge_value(
            base.get(key, _MISSING),
            disk.get(key, _MISSING),
            local.get(key, _MISSING),
            path=f"{path}.{key}" if path else key,
            conflicts=conflicts,
        )
        if child is not _MISSING:
            merged[key] = child
    return merged


def _candidate_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    candidate_id = str(value.get("candidate_id") or value.get("cid") or "").strip()
    if not candidate_id and isinstance(value.get("candidate"), Mapping):
        nested = value["candidate"]
        candidate_id = str(
            nested.get("candidate_id") or nested.get("cid") or ""
        ).strip()
    return candidate_id or None


def _candidate_rows(
    state: Mapping[str, object], fields: Sequence[str]
) -> dict[str, dict[str, list[object]]]:
    rows: dict[str, dict[str, list[object]]] = {}
    for field in fields:
        values = state.get(field, [])
        if not isinstance(values, list):
            continue
        for value in values:
            candidate_id = _candidate_id(value)
            if candidate_id is None:
                continue
            rows.setdefault(candidate_id, {}).setdefault(field, []).append(
                deepcopy(value)
            )
    return rows


def _unkeyed_rows(state: Mapping[str, object], field: str) -> list[object]:
    values = state.get(field, [])
    if not isinstance(values, list):
        return []
    return [deepcopy(value) for value in values if _candidate_id(value) is None]


def _candidate_order(state: Mapping[str, object], field: str) -> list[str]:
    values = state.get(field, [])
    if not isinstance(values, list):
        return []
    return [
        candidate_id
        for value in values
        if (candidate_id := _candidate_id(value)) is not None
    ]


def _choose_candidate_rows(
    base: Mapping[str, object],
    disk: Mapping[str, object],
    local: Mapping[str, object],
    fields: Sequence[str],
    conflicts: list[str],
) -> dict[str, dict[str, list[object]]]:
    base_rows = _candidate_rows(base, fields)
    disk_rows = _candidate_rows(disk, fields)
    local_rows = _candidate_rows(local, fields)
    chosen: dict[str, dict[str, list[object]]] = {}
    for candidate_id in sorted(set(base_rows) | set(disk_rows) | set(local_rows)):
        before = base_rows.get(candidate_id, {})
        on_disk = disk_rows.get(candidate_id, {})
        in_tick = local_rows.get(candidate_id, {})
        if in_tick == before:
            selected = on_disk
        elif on_disk == before or in_tick == on_disk:
            selected = in_tick
        else:
            selected = on_disk
            conflicts.append(f"candidate:{candidate_id}")
        if selected:
            chosen[candidate_id] = deepcopy(selected)
    return chosen


def _merge_candidate_collections(
    base: Mapping[str, object],
    disk: Mapping[str, object],
    local: Mapping[str, object],
    fields: Sequence[str],
    conflicts: list[str],
) -> dict[str, list[object]]:
    chosen = _choose_candidate_rows(base, disk, local, fields, conflicts)
    merged: dict[str, list[object]] = {}
    for field in fields:
        present = _merge_value(
            field in base,
            field in disk,
            field in local,
            path=f"{field}.__present__",
            conflicts=conflicts,
        )
        if present is not True:
            continue
        base_values = base.get(field, [])
        disk_values = disk.get(field, [])
        local_values = local.get(field, [])
        exact_source: object = _MISSING
        if local_values == base_values:
            exact_source = disk_values
        elif disk_values == base_values or local_values == disk_values:
            exact_source = local_values
        unkeyed = _merge_value(
            _unkeyed_rows(base, field),
            _unkeyed_rows(disk, field),
            _unkeyed_rows(local, field),
            path=f"{field}.__unkeyed__",
            conflicts=conflicts,
        )
        chosen_in_field = {
            candidate_id: {field: rows[field]}
            for candidate_id, rows in chosen.items()
            if field in rows
        }
        if (
            isinstance(exact_source, list)
            and _candidate_rows({field: exact_source}, (field,))
            == chosen_in_field
            and _unkeyed_rows({field: exact_source}, field) == unkeyed
        ):
            merged[field] = deepcopy(exact_source)
            continue
        output: list[object] = []
        emitted: set[str] = set()
        order = _merge_value(
            _candidate_order(base, field),
            _candidate_order(disk, field),
            _candidate_order(local, field),
            path=f"{field}.__order__",
            conflicts=conflicts,
        )
        ordered_ids = list(order) if isinstance(order, list) else []
        ordered_ids.extend(
            candidate_id
            for source in (disk, local, base)
            for candidate_id in _candidate_order(source, field)
        )
        for candidate_id in ordered_ids:
            if candidate_id in emitted:
                continue
            selected = chosen.get(candidate_id, {}).get(field)
            if selected:
                output.extend(deepcopy(selected))
                emitted.add(candidate_id)
        for candidate_id in sorted(set(chosen) - emitted):
            selected = chosen[candidate_id].get(field)
            if selected:
                output.extend(deepcopy(selected))
        if isinstance(unkeyed, list):
            output.extend(unkeyed)
        merged[field] = output
    return merged


def merge_runner_state(
    base: Mapping[str, object],
    disk: Mapping[str, object],
    local: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    """Merge one tick snapshot, preferring disk on same-surface conflicts."""

    conflicts: list[str] = []
    fields = tuple(
        field
        for field in CANDIDATE_COLLECTIONS
        if all(
            field not in state or isinstance(state.get(field), list)
            for state in (base, disk, local)
        )
    )
    ignored = set(fields) | {"updated_at"}
    ordinary = _merge_mapping(
        {key: value for key, value in base.items() if key not in ignored},
        {key: value for key, value in disk.items() if key not in ignored},
        {key: value for key, value in local.items() if key not in ignored},
        path="",
        conflicts=conflicts,
    )
    ordinary.update(
        _merge_candidate_collections(base, disk, local, fields, conflicts)
    )
    return ordinary, list(dict.fromkeys(conflicts))


def _read_disk(path: Path, *, base: Mapping[str, object]) -> tuple[dict, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        try:
            backup = json.loads(
                path.with_suffix(".json.bak").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            backup = _MISSING
        if backup == dict(base):
            return deepcopy(dict(base)), None
        if not base and backup is _MISSING:
            return {}, None
        raise RunnerStateWritebackError(
            "RUNNER_STATE_WRITEBACK_DISK_STATE_DISAPPEARED"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RunnerStateWritebackError(
            "RUNNER_STATE_WRITEBACK_DISK_STATE_INVALID"
        ) from exc
    if not isinstance(value, dict):
        raise RunnerStateWritebackError(
            "RUNNER_STATE_WRITEBACK_DISK_STATE_NOT_OBJECT"
        )
    return value, raw


def _atomic_write(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if path.exists():
        os.replace(path, path.with_suffix(".json.bak"))
    os.replace(tmp, path)


def write_state(
    path: Path,
    state: dict,
    *,
    updated_at: str,
    log: Callable[[str], None],
) -> None:
    """Write state atomically, merging a tracked tick against fresh disk bytes."""

    path = Path(path)
    if not isinstance(state, TrackedState) or state._writeback_path != path:
        state["updated_at"] = updated_at
        _atomic_write(path, state)
        return

    base = deepcopy(state._writeback_base)
    local = deepcopy(dict(state))
    for _attempt in range(MAX_CAS_ATTEMPTS):
        disk, before = _read_disk(path, base=base)
        merged, conflicts = merge_runner_state(base, disk, local)
        merged["updated_at"] = updated_at
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != before:
            continue
        _atomic_write(path, merged)
        state.accept_writeback(merged)
        for conflict in conflicts:
            log(
                "RUNNER_STATE_WRITEBACK_CONFLICT "
                f"date={path.stem} scope={conflict} resolution=disk_wins"
            )
        return
    raise RunnerStateWritebackError(
        "RUNNER_STATE_WRITEBACK_CAS_RETRY_EXHAUSTED"
    )
