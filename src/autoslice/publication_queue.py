"""Pipeline-owned, manifest-bound serial publication queue consumer.

This module never creates authorization or review evidence. It may prepare one
v3 manifest from a source-bound project authority, or consume one already-ready
manifest. At most one action runs per invocation; every upload mutation remains
delegated to ``scripts.authorized_upload`` so the canonical lock, ledger, quota,
season and public-verification state machines stay authoritative.
"""

from __future__ import annotations

import os
import hashlib
import json
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.autoslice.publication_manifest_prepare import (
    prepare_manifest_from_readiness,
)
from src.autoslice import publication_queue_backoff
from src.autoslice.upload_ledger_scope import content_identity, guard_manifest
from src.autoslice.publication_readiness import (
    READY_FOR_SERIAL_UPLOAD,
    READY_TO_PREPARE,
    build_readiness_graph,
)

RESULT_SCHEMA_VERSION = "pipeline-publication-queue-result.v1"
_ACTION_UPLOAD = "UPLOAD"
_ACTION_SEASON_ADD = "SEASON_ADD"
_ACTION_PREPARE_MANIFEST = "PREPARE_MANIFEST"


def _result(status: str, **extra: object) -> dict[str, object]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": status,
        **extra,
    }


def _safe_runtime_root(value: str | Path) -> Path:
    raw = Path(value).expanduser().absolute()
    try:
        info = os.lstat(raw)
    except OSError as exc:
        raise ValueError("publication runtime root is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("publication runtime root must be a non-symlink directory")
    return raw


def _contained_manifest(value: object, *, runtime_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("publication action has no manifest path")
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("publication manifest path is unsafe")
    try:
        resolved = raw.resolve(strict=True)
        resolved.relative_to(runtime_root)
        info = raw.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("publication manifest is unavailable or outside runtime") from exc
    if not resolved.is_file() or info.st_nlink != 1:
        raise ValueError("publication manifest must be a single-link regular file")
    return resolved


def _resolve_ledger_accessors(
    ledger_reader: Callable[[Path], tuple[list[dict], list[str]]] | None,
    ledger_guard: Callable[
        [Path, str], tuple[str | None, dict | None, list[str]]
    ]
    | None,
) -> tuple[
    Callable[[Path], tuple[list[dict], list[str]]],
    Callable[[Path, str], tuple[str | None, dict | None, list[str]]],
]:
    """Resolve the canonical append-only ledger consumers once per queue tick."""

    if ledger_reader is None or ledger_guard is None:
        from scripts import authorized_upload as uploader

        ledger_reader = ledger_reader or uploader.read_ledger
        ledger_guard = ledger_guard or uploader.ledger_guard
    return ledger_reader, ledger_guard


def _followup_media_available(manifest: Path, runtime_root: Path) -> None:
    """Do not let a retired v3 product monopolize the follow-up queue.

    This is only a local availability check. The canonical uploader still
    verifies hashes, package quality, ledger binding and the existing BV.
    """
    try:
        if manifest.stat().st_size > 2 << 20:
            raise ValueError("follow-up manifest is too large")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError("follow-up manifest is not an object")
        if document.get("manifest_version") != 3:
            return  # Legacy recovery retains the canonical uploader's contract.
        for role in ("video", "cover"):
            entry = document.get(role)
            value = entry.get("path") if isinstance(entry, Mapping) else None
            if not isinstance(value, str) or not value:
                raise ValueError(f"follow-up {role} path is missing")
            path = Path(value)
            if (not path.is_absolute() or path.is_symlink()
                    or not path.resolve().is_relative_to(runtime_root)
                    or not path.is_file()):
                raise ValueError(f"follow-up {role} is unavailable or outside runtime")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("follow-up manifest is unreadable") from exc


def _canonical_action_ledger_commit(
    ledger: Path,
    action: Mapping[str, object],
    *,
    ledger_reader: Callable[[Path], tuple[list[dict], list[str]]],
    ledger_guard: Callable[
        [Path, str], tuple[str | None, dict | None, list[str]]
    ],
) -> dict[str, object]:
    """Classify a canonical action from its durable, manifest-bound ledger state."""

    entries, problems = ledger_reader(ledger)
    if problems:
        return {
            "status": "INVALID",
            "reason_code": "UPLOAD_LEDGER_INVALID",
            "detail": problems[:16],
        }
    manifest = action.get("manifest")
    if not isinstance(manifest, str) or not manifest:
        return {
            "status": "UNVERIFIED",
            "reason_code": "PUBLICATION_ACTION_OUTCOME_UNVERIFIED",
            "detail": "successful action has no manifest identity",
        }
    expected_video_sha = action.get("video_sha256")
    matching_hashes: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("manifest") != manifest:
            continue
        value = entry.get("video_sha256")
        if isinstance(value, str) and value and value not in matching_hashes:
            matching_hashes.append(value)
    if isinstance(expected_video_sha, str) and expected_video_sha:
        if matching_hashes and matching_hashes != [expected_video_sha]:
            return {
                "status": "INVALID",
                "reason_code": "UPLOAD_LEDGER_INVALID",
                "detail": "successful action manifest maps to conflicting video hashes",
            }
    elif len(matching_hashes) == 1:
        expected_video_sha = matching_hashes[0]
    elif not matching_hashes:
        return {
            "status": "UNVERIFIED",
            "reason_code": "PUBLICATION_ACTION_OUTCOME_UNVERIFIED",
            "detail": "successful action wrote no matching ledger identity",
        }
    else:
        return {
            "status": "INVALID",
            "reason_code": "UPLOAD_LEDGER_INVALID",
            "detail": "successful action manifest maps to multiple video hashes",
        }
    status, row, guard_problems = ledger_guard(ledger, expected_video_sha)
    if guard_problems:
        return {
            "status": "INVALID",
            "reason_code": "UPLOAD_LEDGER_INVALID",
            "detail": guard_problems[:16],
        }
    if status not in {"uploaded", "posted_unverified", "unresolved"}:
        return {
            "status": "UNVERIFIED",
            "reason_code": "PUBLICATION_ACTION_OUTCOME_UNVERIFIED",
            "detail": f"successful action ledger status is {status or 'absent'}",
            "video_sha256": expected_video_sha,
        }
    if not isinstance(row, Mapping) or row.get("manifest") != manifest:
        return {
            "status": "UNVERIFIED",
            "reason_code": "PUBLICATION_ACTION_OUTCOME_UNVERIFIED",
            "detail": "successful action ledger row belongs to another manifest",
            "video_sha256": expected_video_sha,
        }
    expected_bvid = action.get("bvid")
    if (
        status in {"uploaded", "posted_unverified"}
        and isinstance(expected_bvid, str)
        and expected_bvid
        and row.get("bvid") != expected_bvid
    ):
        return {
            "status": "UNVERIFIED",
            "reason_code": "PUBLICATION_ACTION_OUTCOME_UNVERIFIED",
            "detail": "successful season recovery ledger BVID does not match the action",
            "video_sha256": expected_video_sha,
        }
    if status == "unresolved":
        return {
            "status": "UNRESOLVED",
            "reason_code": "UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED",
            "detail": "successful action left an unresolved upload attempt",
            "video_sha256": expected_video_sha,
            "attempt_id": row.get("attempt_id"),
            "ledger_entry_count": len(entries),
        }
    if status == "posted_unverified":
        return {
            "status": "POSTED_UNVERIFIED",
            "reason_code": "PUBLICATION_ACTION_OUTCOME_UNVERIFIED",
            "detail": "successful action ledger status is posted_unverified",
            "video_sha256": expected_video_sha,
            "attempt_id": row.get("attempt_id"),
            "bvid": row.get("bvid"),
            "terminal_event": row.get("event"),
            "ledger_entry_count": len(entries),
        }
    return {
        "status": "COMMITTED",
        "video_sha256": expected_video_sha,
        "attempt_id": row.get("attempt_id"),
        "bvid": row.get("bvid"),
        "terminal_event": row.get("event"),
        "ledger_entry_count": len(entries),
    }


def _ledger_dependencies(
    ledger: Path,
    *,
    ledger_reader: Callable[[Path], tuple[list[dict], list[str]]],
    ledger_guard: Callable[
        [Path, str], tuple[str | None, dict | None, list[str]]
    ],
    runtime_root: Path,
    quarantined: list[dict[str, object]],
) -> dict[str, object] | None:
    entries, problems = ledger_reader(ledger)
    if problems:
        return _result(
            "BLOCKED_LEDGER",
            reason_codes=["UPLOAD_LEDGER_INVALID"],
            detail=problems[:16],
        )
    video_hashes: list[str] = []
    for entry in entries:
        value = entry.get("video_sha256") if isinstance(entry, Mapping) else None
        if not isinstance(value, str) or not value:
            return _result("BLOCKED_LEDGER", reason_codes=["UPLOAD_LEDGER_INVALID"],
                           detail=["upload ledger row has no content identifier"])
        if isinstance(value, str) and value and value not in video_hashes:
            video_hashes.append(value)
    guarded: list[tuple[str, str | None, dict | None]] = []
    all_guard_problems: list[str] = []
    for video_sha256 in video_hashes:
        status, row, guard_problems = ledger_guard(ledger, video_sha256)
        guarded.append((video_sha256, status, row))
        all_guard_problems.extend(guard_problems)
    if all_guard_problems:
        return _result(
            "BLOCKED_LEDGER",
            reason_codes=["UPLOAD_LEDGER_INVALID"],
            detail=all_guard_problems[:16],
        )
    for _video_sha256, status, row in guarded:
        if status == "unresolved":
            quarantined.append({
                "reason_code": "UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED",
                "video_sha256": _video_sha256,
                "attempt_id": (row or {}).get("attempt_id"),
            })
    for video_sha256, status, row in guarded:
        if status == "posted_unverified":
            if not isinstance(row, Mapping):
                return _result(
                    "BLOCKED_LEDGER",
                    reason_codes=["UPLOAD_LEDGER_INVALID"],
                )
            bvid = row.get("bvid")
            if not isinstance(bvid, str) or not bvid:
                return _result(
                    "BLOCKED_LEDGER",
                    reason_codes=["POSTED_ARCHIVE_BVID_MISSING"],
                )
            try:
                manifest = _contained_manifest(
                    row.get("manifest"), runtime_root=runtime_root
                )
                _followup_media_available(manifest, runtime_root)
            except ValueError as exc:
                quarantined.append({
                    "reason_code": "POSTED_ARCHIVE_MANIFEST_INVALID",
                    "video_sha256": video_sha256,
                    "attempt_id": row.get("attempt_id"),
                    "detail": str(exc),
                })
                continue
            return {
                "action": _ACTION_SEASON_ADD,
                "manifest": str(manifest),
                "bvid": bvid,
                "video_sha256": video_sha256,
                "attempt_id": row.get("attempt_id"),
            }
    return None


def _ready_upload_actions(
    graph: Mapping[str, object],
    *,
    runtime_root: Path,
    quarantined: list[dict[str, object]],
) -> list[dict[str, object]] | dict[str, object]:
    """Return ready uploads in the authoritative ready-first order.

    维护者's rule prioritizes timeliness inside the *currently
    actionable* ready set. A manifest-local retry delay removes only that item
    from the current actionable set; account-global blockers remain global.
    """

    blockers = graph.get("graph_blockers")
    if isinstance(blockers, list) and blockers:
        return _result(
            "BLOCKED_GRAPH",
            reason_codes=["PUBLICATION_READINESS_GRAPH_BLOCKED"],
            graph_blockers=blockers[:32],
        )
    rows = graph.get("rows")
    if not isinstance(rows, list):
        return _result(
            "BLOCKED_GRAPH",
            reason_codes=["PUBLICATION_READINESS_GRAPH_INVALID"],
        )
    ready: list[tuple[int, Mapping[str, object]]] = [
        (index, row)
        for index, row in enumerate(rows)
        if isinstance(row, Mapping)
        and row.get("category") == READY_FOR_SERIAL_UPLOAD
    ]
    ready.sort(
        key=lambda pair: (
            str(pair[1].get("recording_date") or ""),
            -pair[0],
        ),
        reverse=True,
    )
    actions: list[dict[str, object]] = []
    for _index, row in ready:
        dependencies = row.get("package_dependencies")
        manifest_value = (
            dependencies.get("upload_manifest")
            if isinstance(dependencies, Mapping)
            else None
        )
        try:
            manifest = _contained_manifest(
                manifest_value,
                runtime_root=runtime_root,
            )
        except ValueError as exc:
            quarantined.append({"reason_code": "READY_UPLOAD_MANIFEST_INVALID",
                                "candidate_id": row.get("candidate_id"), "detail": str(exc)})
            continue
        actions.append(
            {
                "action": _ACTION_UPLOAD,
                "manifest": str(manifest),
                "candidate_id": row.get("candidate_id"),
                "recording_date": row.get("recording_date"),
                "lane": row.get("lane"),
            }
        )
    if ready and not actions:
        return _result("BLOCKED_GRAPH", reason_codes=["READY_UPLOAD_MANIFEST_INVALID"],
                       quarantined_attempts=quarantined)
    return actions


def _ready_prepare_action(
    graph: Mapping[str, object],
    *, excluded: set[tuple[str, str]] | None = None,
) -> dict[str, object] | None:
    rows = graph.get("rows")
    if not isinstance(rows, list):
        return _result(
            "BLOCKED_GRAPH",
            reason_codes=["PUBLICATION_READINESS_GRAPH_INVALID"],
        )
    ready: list[tuple[int, Mapping[str, object]]] = [
        (index, row)
        for index, row in enumerate(rows)
        if isinstance(row, Mapping) and row.get("category") == READY_TO_PREPARE
        and (str(row.get("candidate_id") or ""), str(row.get("recording_date") or "")) not in (excluded or set())
    ]
    if not ready:
        return None
    ready.sort(
        key=lambda pair: (
            str(pair[1].get("recording_date") or ""),
            -pair[0],
        ),
        reverse=True,
    )
    _index, row = ready[0]
    return {
        "action": _ACTION_PREPARE_MANIFEST,
        "candidate_id": row.get("candidate_id"),
        "recording_date": row.get("recording_date"),
        "lane": row.get("lane"),
        "readiness_row": dict(row),
    }


def plan_next_publication_action(
    *,
    repository_root: str | Path,
    runtime_root: str | Path,
    graph_builder: Callable[..., dict[str, Any]] = build_readiness_graph,
    ledger_path: str | Path | None = None,
    ledger_reader: Callable[[Path], tuple[list[dict], list[str]]] | None = None,
    ledger_guard: Callable[
        [Path, str], tuple[str | None, dict | None, list[str]]
    ]
    | None = None,
    backoff_evaluator: Callable[..., dict[str, object]] = (
        publication_queue_backoff.evaluate_upload_backoff
    ),
    now_epoch: float | None = None,
) -> dict[str, object]:
    root = _safe_runtime_root(runtime_root)
    ledger = (
        Path(ledger_path).expanduser().absolute()
        if ledger_path is not None
        else root / "reports" / "upload_ledger.jsonl"
    )
    ledger_reader, ledger_guard = _resolve_ledger_accessors(
        ledger_reader,
        ledger_guard,
    )
    quarantined: list[dict[str, object]] = []
    recovery = _ledger_dependencies(
        ledger,
        ledger_reader=ledger_reader,
        ledger_guard=ledger_guard,
        runtime_root=root,
        quarantined=quarantined,
    )
    if recovery is not None:
        if "action" in recovery:
            return _result(
                "ACTION_READY",
                action=recovery,
                ledger=str(ledger),
                quarantined_attempts=quarantined,
            )
        return recovery
    graph = graph_builder(
        repository_root=Path(repository_root).expanduser().absolute(),
        runtime_root=root,
    )
    upload_actions = _ready_upload_actions(graph, runtime_root=root, quarantined=quarantined)

    def prepare_action():
        # Check each ready product's own identity before creating a manifest.
        # A hold on one product must not monopolize the preparation queue.
        excluded: set[tuple[str, str]] = set()
        entries, problems = ledger_reader(ledger)
        if problems:
            return _result("BLOCKED_LEDGER", reason_codes=["UPLOAD_LEDGER_INVALID"], detail=problems[:16])
        while (action := _ready_prepare_action(graph, excluded=excluded)) is not None:
            if "action" not in action or not entries:
                return action
            row = action["readiness_row"]
            try:
                dependencies = row.get("package_dependencies") or {}
                if not isinstance(dependencies, Mapping):
                    raise ValueError("preparation dependencies are not an object")
                record_path = _contained_manifest(dependencies.get("record"), runtime_root=root)
                if record_path.stat().st_size > 2 << 20:
                    raise ValueError("preparation record is too large")
                record_bytes = record_path.read_bytes()
                # This is only an observational hold query. make-manifest still
                # replays every ordinary quality and authorization gate.
                query = {"video": {}, "package_attestation": {
                    "package_root": str(record_path.parent),
                    "record": {"path": str(record_path), "sha256": hashlib.sha256(record_bytes).hexdigest()},
                }}
                content_identity(query)  # Invalid product input is a local hold.
                status, held_row, guard_problems = guard_manifest(ledger_guard, ledger, "", query)
                if guard_problems:
                    return _result("BLOCKED_LEDGER", reason_codes=["UPLOAD_LEDGER_INVALID"], detail=guard_problems[:16])
                if status is None:
                    return action
                quarantined.append({"candidate_id": action.get("candidate_id"),
                                    "reason_code": "UPLOAD_CONTENT_HELD", "ledger_status": status,
                                    "attempt_id": (held_row or {}).get("attempt_id")})
            except (OSError, ValueError, TypeError) as exc:
                quarantined.append({"candidate_id": action.get("candidate_id"),
                                    "reason_code": "PREPARATION_INPUT_INVALID", "detail": str(exc)})
            excluded.add((str(action.get("candidate_id") or ""), str(action.get("recording_date") or "")))
        return None
    if isinstance(upload_actions, dict):
        if upload_actions.get("reason_codes") == ["READY_UPLOAD_MANIFEST_INVALID"]:
            next_prepare = prepare_action()
            if next_prepare is not None:
                if "action" not in next_prepare:
                    return next_prepare
                return _result("ACTION_READY", action=next_prepare, ledger=str(ledger),
                               quarantined_attempts=quarantined)
        return upload_actions
    if upload_actions:
        entries, backoff_ledger_problems = ledger_reader(ledger)
        if backoff_ledger_problems:
            return _result(
                "BLOCKED_LEDGER",
                reason_codes=["UPLOAD_LEDGER_INVALID"],
                detail=backoff_ledger_problems[:16],
            )
        manifest_backoffs: list[dict[str, object]] = []
        scoped_holds: list[dict[str, object]] = []
        global_backoff: dict[str, object] | None = None
        for action in upload_actions:
            if entries:
                try:
                    manifest = json.loads(Path(action["manifest"]).read_text(encoding="utf-8"))
                    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("video"), Mapping):
                        raise ValueError("upload manifest/video is not an object")
                    content_identity(manifest)
                    status, row, guard_problems = guard_manifest(
                        ledger_guard, ledger, str((manifest.get("video") or {}).get("sha256") or ""), manifest,
                    )
                except (OSError, ValueError, TypeError) as exc:
                    scoped_holds.append({"action": action, "reason_code": "READY_UPLOAD_MANIFEST_INVALID", "detail": str(exc)})
                    continue
                if guard_problems:
                    return _result("BLOCKED_LEDGER", reason_codes=["UPLOAD_LEDGER_INVALID"], detail=guard_problems[:16])
                if status in {"unresolved", "uploaded", "posted_unverified"}:
                    scoped_holds.append({"action": action, "reason_code": "UPLOAD_CONTENT_HELD",
                                         "attempt_id": (row or {}).get("attempt_id"), "ledger_status": status})
                    continue
            backoff = backoff_evaluator(
                runtime_root=root,
                manifest_path=action["manifest"],
                ledger_entries=entries,
                now_epoch=now_epoch,
            )
            if backoff.get("status") == "INVALID":
                return _result(
                    "BLOCKED_BACKOFF_STATE",
                    reason_codes=["PUBLICATION_QUEUE_BACKOFF_STATE_INVALID"],
                    candidate_id=action.get("candidate_id"),
                    detail=backoff.get("detail"),
                )
            if backoff.get("status") == "BACKOFF":
                blocked = {"action": action, "backoff": backoff}
                manifest_backoffs.append(blocked)
                if backoff.get("scope") == "GLOBAL_QUOTA":
                    global_backoff = blocked
                    break
                continue
            return _result(
                "ACTION_READY",
                action=action,
                ledger=str(ledger),
                skipped_backoff=manifest_backoffs,
                quarantined_attempts=quarantined,
                skipped_content_holds=scoped_holds,
            )
        next_prepare = prepare_action()
        if isinstance(next_prepare, Mapping) and "action" not in next_prepare:
            return dict(next_prepare)
        if (
            isinstance(next_prepare, Mapping)
            and "action" in next_prepare
        ):
            return _result(
                "ACTION_READY",
                action=next_prepare,
                ledger=str(ledger),
                skipped_backoff=manifest_backoffs,
                quarantined_attempts=quarantined,
                skipped_content_holds=scoped_holds,
                deferred_upload_backoff=(
                    global_backoff["backoff"]
                    if global_backoff is not None
                    else None
                ),
            )
        if global_backoff is not None:
            return _result(
                "RETRY_BACKOFF",
                action=global_backoff["action"],
                ledger=str(ledger),
                backoff=global_backoff["backoff"],
                backed_off_candidates=manifest_backoffs,
            )
        if not manifest_backoffs:
            return _result("BLOCKED_CONTENT", reason_codes=["UPLOAD_CONTENT_HELD"],
                           skipped_content_holds=scoped_holds, quarantined_attempts=quarantined)
        selected = min(
            manifest_backoffs,
            key=lambda item: float(
                (item.get("backoff") or {}).get("next_attempt_epoch")
                or float("inf")
            ),
        )
        return _result(
            "RETRY_BACKOFF",
            action=selected["action"],
            ledger=str(ledger),
            backoff=selected["backoff"],
            backed_off_candidates=manifest_backoffs,
        )
    action = prepare_action()
    if action is None:
        return _result("NO_READY_ACTION", ledger=str(ledger), quarantined_attempts=quarantined)
    if "action" not in action:
        return action
    return _result("ACTION_READY", action=action, ledger=str(ledger), quarantined_attempts=quarantined)


def consume_ready_publication_queue(
    *,
    repository_root: str | Path,
    runtime_root: str | Path,
    enabled: bool,
    graph_builder: Callable[..., dict[str, Any]] = build_readiness_graph,
    upload_call: Callable[[list[str]], int] | None = None,
    manifest_prepare_call: Callable[..., dict[str, object]] = (
        prepare_manifest_from_readiness
    ),
    ledger_path: str | Path | None = None,
    ledger_reader: Callable[[Path], tuple[list[dict], list[str]]] | None = None,
    ledger_guard: Callable[
        [Path, str], tuple[str | None, dict | None, list[str]]
    ]
    | None = None,
    backoff_evaluator: Callable[..., dict[str, object]] = (
        publication_queue_backoff.evaluate_upload_backoff
    ),
    backoff_recorder: Callable[..., dict[str, object]] = (
        publication_queue_backoff.record_queue_result
    ),
    now_epoch: float | None = None,
) -> dict[str, object]:
    """Serialize a whole queue tick across the timer and ordinary runner."""

    if enabled is not True:
        return _result("DISABLED", side_effect_attempted=False)
    try:
        root = _safe_runtime_root(runtime_root)
    except ValueError as exc:
        return _result("BLOCKED_RUNTIME", reason_codes=["PUBLICATION_RUNTIME_INVALID"],
                       detail=str(exc), side_effect_attempted=False)
    from scripts.authorized_upload import UploadLockBusy, exclusive_upload_lock

    # The uploader still owns upload.lock. This outer lock also covers manifest
    # preparation, readiness selection and follow-up recovery, across all callers.
    try:
        with exclusive_upload_lock(root / "publication-queue.lock"):
            return _consume_ready_publication_queue_locked(
                repository_root=repository_root, runtime_root=root, enabled=True,
                graph_builder=graph_builder, upload_call=upload_call,
                manifest_prepare_call=manifest_prepare_call, ledger_path=ledger_path,
                ledger_reader=ledger_reader, ledger_guard=ledger_guard,
                backoff_evaluator=backoff_evaluator, backoff_recorder=backoff_recorder,
                now_epoch=now_epoch,
            )
    except UploadLockBusy:
        return _result("QUEUE_BUSY", reason_codes=["PUBLICATION_QUEUE_LOCK_BUSY"],
                       side_effect_attempted=False)


def _runtime_pause(root: Path) -> dict[str, object] | None:
    for marker, reason_code in (
        ("PUBLISH_PAUSED", "PUBLICATION_PAUSED"),
        ("deploy.guard", "DEPLOYMENT_IN_PROGRESS"),
        ("AUTO_UPLOAD", "LEGACY_AUTO_UPLOAD_MARKER_PRESENT"),
    ):
        path = root / marker
        if path.exists() or path.is_symlink():
            return _result("BLOCKED_RUNTIME", reason_codes=[reason_code],
                           marker=str(path), side_effect_attempted=False)
    return None


def _consume_ready_publication_queue_locked(
    *,
    repository_root: str | Path,
    runtime_root: str | Path,
    enabled: bool,
    graph_builder: Callable[..., dict[str, Any]] = build_readiness_graph,
    upload_call: Callable[[list[str]], int] | None = None,
    manifest_prepare_call: Callable[..., dict[str, object]] = (
        prepare_manifest_from_readiness
    ),
    ledger_path: str | Path | None = None,
    ledger_reader: Callable[[Path], tuple[list[dict], list[str]]] | None = None,
    ledger_guard: Callable[
        [Path, str], tuple[str | None, dict | None, list[str]]
    ]
    | None = None,
    backoff_evaluator: Callable[..., dict[str, object]] = (
        publication_queue_backoff.evaluate_upload_backoff
    ),
    backoff_recorder: Callable[..., dict[str, object]] = (
        publication_queue_backoff.record_queue_result
    ),
    now_epoch: float | None = None,
) -> dict[str, object]:
    """Execute at most one manifest-bound publication action."""

    if enabled is not True:
        return _result("DISABLED", side_effect_attempted=False)
    try:
        root = _safe_runtime_root(runtime_root)
    except ValueError as exc:
        return _result(
            "BLOCKED_RUNTIME",
            reason_codes=["PUBLICATION_RUNTIME_INVALID"],
            detail=str(exc),
            side_effect_attempted=False,
        )
    ledger_reader, ledger_guard = _resolve_ledger_accessors(
        ledger_reader,
        ledger_guard,
    )
    canonical_upload_call = upload_call is None
    pause = _runtime_pause(root)
    if pause is not None:
        return pause
    plan = plan_next_publication_action(
        repository_root=repository_root,
        runtime_root=root,
        graph_builder=graph_builder,
        ledger_path=ledger_path,
        ledger_reader=ledger_reader,
        ledger_guard=ledger_guard,
        backoff_evaluator=backoff_evaluator,
        now_epoch=now_epoch,
    )
    if plan.get("status") != "ACTION_READY":
        return {**plan, "side_effect_attempted": False}
    action = plan.get("action")
    skipped_backoff = plan.get("skipped_backoff")
    if not isinstance(skipped_backoff, list):
        skipped_backoff = []
    deferred_upload_backoff = plan.get("deferred_upload_backoff")
    if not isinstance(deferred_upload_backoff, Mapping):
        deferred_upload_backoff = None
    if not isinstance(action, Mapping):
        return _result(
            "BLOCKED_GRAPH",
            reason_codes=["PUBLICATION_ACTION_INVALID"],
            side_effect_attempted=False,
        )
    # Readiness may hash large media. Honor a pause arriving during that work.
    pause = _runtime_pause(root)
    if pause is not None:
        return pause
    if action.get("action") == _ACTION_PREPARE_MANIFEST:
        readiness_row = action.get("readiness_row")
        if not isinstance(readiness_row, Mapping):
            return _result(
                "BLOCKED_GRAPH",
                reason_codes=["PUBLICATION_ACTION_INVALID"],
                side_effect_attempted=False,
            )
        prepared = manifest_prepare_call(
            readiness_row,
            repository_root=repository_root,
            runtime_root=root,
        )
        return _result(
            "MANIFEST_PREPARED"
            if prepared.get("status") == "PREPARED"
            else "MANIFEST_PREPARATION_BLOCKED",
            action=dict(action),
            preparation=prepared,
            ledger=str(plan["ledger"]),
            skipped_backoff=skipped_backoff,
            deferred_upload_backoff=deferred_upload_backoff,
            side_effect_attempted=True,
            external_side_effect_attempted=False,
            quarantined_attempts=plan.get("quarantined_attempts", []),
            skipped_content_holds=plan.get("skipped_content_holds", []),
        )
    if upload_call is None:
        from scripts import authorized_upload as uploader

        upload_call = uploader.main
    ledger = str(plan["ledger"])
    lock = str(root / "upload.lock")
    if action.get("action") == _ACTION_UPLOAD:
        argv = [
            "upload",
            "--manifest",
            str(action["manifest"]),
            "--ledger",
            ledger,
            "--lock",
            lock,
        ]
    elif action.get("action") == _ACTION_SEASON_ADD:
        argv = [
            "season-add",
            "--manifest",
            str(action["manifest"]),
            "--bvid",
            str(action["bvid"]),
            "--ledger",
            ledger,
        ]
    else:
        return _result(
            "BLOCKED_GRAPH",
            reason_codes=["PUBLICATION_ACTION_INVALID"],
            side_effect_attempted=False,
        )
    rc = upload_call(argv)
    statuses = {
        0: "ACTION_COMPLETED",
        2: "VALIDATION_BLOCKED",
        3: "DUPLICATE_ALREADY_UPLOADED",
        5: "LEDGER_BLOCKED",
        6: "FOLLOWUP_PENDING",
        8: "QUOTA_BLOCKED",
    }
    action_status = statuses.get(rc, "ACTION_FAILED")
    ledger_commit: dict[str, object] | None = None
    committed_entries: list[dict] | None = None
    if canonical_upload_call and rc in {0, 6}:
        ledger_commit = _canonical_action_ledger_commit(
            Path(ledger),
            action,
            ledger_reader=ledger_reader,
            ledger_guard=ledger_guard,
        )
        durable_status = ledger_commit.get("status")
        if durable_status == "COMMITTED":
            action_status = "ACTION_COMPLETED"
        elif rc == 6 and durable_status == "POSTED_UNVERIFIED":
            action_status = "FOLLOWUP_PENDING"
        else:
            return _result(
                "LEDGER_BLOCKED",
                reason_codes=[str(ledger_commit["reason_code"])],
                action=dict(action),
                action_result_status=action_status,
                argv=argv,
                rc=rc,
                ledger=ledger,
                ledger_commit=ledger_commit,
                skipped_backoff=skipped_backoff,
                side_effect_attempted=True,
                external_side_effect_attempted=True,
            )
        committed_entries, committed_problems = ledger_reader(Path(ledger))
        if committed_problems:
            return _result(
                "LEDGER_BLOCKED",
                reason_codes=["UPLOAD_LEDGER_INVALID"],
                action=dict(action),
                action_result_status=action_status,
                argv=argv,
                rc=rc,
                ledger=ledger,
                ledger_commit={
                    "status": "INVALID",
                    "reason_code": "UPLOAD_LEDGER_INVALID",
                    "detail": committed_problems[:16],
                },
                skipped_backoff=skipped_backoff,
                side_effect_attempted=True,
                external_side_effect_attempted=True,
            )
    backoff_state: dict[str, object] | None = None
    if action.get("action") == _ACTION_UPLOAD:
        if committed_entries is None:
            entries, backoff_ledger_problems = ledger_reader(Path(ledger))
        else:
            entries, backoff_ledger_problems = committed_entries, []
        if backoff_ledger_problems:
            backoff_state = {
                "status": "INVALID",
                "reason_code": "UPLOAD_LEDGER_INVALID",
                "detail": backoff_ledger_problems[:16],
            }
        else:
            backoff_state = backoff_recorder(
                runtime_root=root,
                manifest_path=action["manifest"],
                rc=rc,
                status=action_status,
                ledger_entries=entries,
                now_epoch=now_epoch,
            )
        if backoff_state.get("status") == "INVALID":
            return _result(
                "BACKOFF_STATE_INVALID",
                action=dict(action),
                action_result_status=action_status,
                argv=argv,
                rc=rc,
                ledger=ledger,
                backoff_state=backoff_state,
                skipped_backoff=skipped_backoff,
                side_effect_attempted=True,
                external_side_effect_attempted=True,
            )
    return _result(
        action_status,
        action=dict(action),
        argv=argv,
        rc=rc,
        ledger=ledger,
        backoff_state=backoff_state,
        ledger_commit=ledger_commit,
        skipped_backoff=skipped_backoff,
        quarantined_attempts=plan.get("quarantined_attempts", []),
        skipped_content_holds=plan.get("skipped_content_holds", []),
        side_effect_attempted=True,
        external_side_effect_attempted=True,
    )


__all__ = [
    "RESULT_SCHEMA_VERSION",
    "consume_ready_publication_queue",
    "plan_next_publication_action",
]
