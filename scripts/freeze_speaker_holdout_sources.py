#!/usr/bin/env python3
"""Freeze sealed raw sessions as source-only speaker holdout evidence.

The output deliberately stops at SOURCE_FROZEN.  It contains no ASR, cue grid,
speaker prediction, or human truth and never writes production state/out paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence


SCHEMA_VERSION = "speaker-holdout-source-freeze.v0"
PURPOSE = "LOCKED_SOURCE_ONLY_NO_ASR_PREDICTION_TRUTH_OR_PRODUCTION_AUTHORITY"
ROOM_ID = "22966160"
STATUS_MAX_AGE_SECONDS = 180
ALLOWED_ROLES = {
    "HOLDOUT_A_SOURCE_FROZEN_UNLABELED",
    "HOLDOUT_B_SOURCE_FROZEN_UNLABELED",
}


class HoldoutSourceFreezeError(RuntimeError):
    pass


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HoldoutSourceFreezeError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise HoldoutSourceFreezeError(f"{label} is not a regular non-symlink file: {path}")
    if path.resolve() != path:
        raise HoldoutSourceFreezeError(f"{label} resolves through an unexpected path: {path}")
    return metadata


def _parse_generated_epoch(value: object) -> float:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise HoldoutSourceFreezeError("recording status generated_at is malformed") from exc
    if parsed.tzinfo is None:
        raise HoldoutSourceFreezeError("recording status generated_at has no timezone")
    return parsed.timestamp()


def _validate_idle_status(
    status_path: Path, *, now_epoch: float
) -> tuple[dict[str, object], dict[str, object]]:
    _regular_file(status_path, label="recording status")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise HoldoutSourceFreezeError("recording status root is not an object")
    generated_epoch = _parse_generated_epoch(payload.get("generated_at"))
    age = now_epoch - generated_epoch
    if age < -5 or age > STATUS_MAX_AGE_SECONDS:
        raise HoldoutSourceFreezeError("recording status is not fresh")
    if str(payload.get("room_id") or "") != ROOM_ID:
        raise HoldoutSourceFreezeError("recording status is not sealed idle: room_id")
    expected = {
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "service_reachable": True,
        "running_status": "idle",
        "error": None,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise HoldoutSourceFreezeError(f"recording status is not sealed idle: {key}")
    deterministic = {
        "room_id": ROOM_ID,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "service_reachable": True,
        "running_status": "idle",
        "error": None,
    }
    observation = {
        "status_path": str(status_path),
        "status_sha256": _sha256(status_path),
        "status_generated_at": payload.get("generated_at"),
        "status_age_seconds": round(age, 3),
    }
    return deterministic, observation


def _cloudfs_mount_evidence(recording_root: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["findmnt", "-T", str(recording_root), "-n", "-o", "TARGET,SOURCE,FSTYPE"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise HoldoutSourceFreezeError("recording root mount could not be resolved")
    fields = completed.stdout.strip().split()
    if len(fields) != 3:
        raise HoldoutSourceFreezeError("recording root mount evidence is malformed")
    target, source, filesystem_type = fields
    if source != "CloudFS" or not filesystem_type.startswith("fuse"):
        raise HoldoutSourceFreezeError("recording root is not the CloudFS FUSE mount")
    if not str(recording_root).startswith(target.rstrip("/") + "/"):
        raise HoldoutSourceFreezeError("recording root is outside the resolved CloudFS target")
    return {"target": target, "source": source, "filesystem_type": filesystem_type}


def _load_adapter_state(path: Path) -> tuple[Mapping[str, object], str]:
    _regular_file(path, label="adapter state")
    payload = json.loads(path.read_text(encoding="utf-8"))
    finalized = payload.get("finalized") if isinstance(payload, Mapping) else None
    if not isinstance(finalized, Mapping):
        raise HoldoutSourceFreezeError("adapter state has no finalized mapping")
    return finalized, _sha256(path)


def _load_file_closed_events(path: Path) -> tuple[dict[str, list[dict[str, object]]], str]:
    _regular_file(path, label="webhook journal")
    events: dict[str, list[dict[str, object]]] = {}
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HoldoutSourceFreezeError(
                    f"webhook journal line {line_number} is malformed"
                ) from exc
            if not isinstance(payload, Mapping) or payload.get("EventType") != "FileClosed":
                continue
            data = payload.get("EventData")
            if not isinstance(data, Mapping):
                raise HoldoutSourceFreezeError("FileClosed event has no EventData")
            relative_path = str(data.get("RelativePath") or "").strip()
            if not relative_path:
                raise HoldoutSourceFreezeError("FileClosed event has no relative path")
            events.setdefault(relative_path, []).append(dict(payload))
    return events, _sha256(path)


def _ffprobe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise HoldoutSourceFreezeError(f"ffprobe failed for {path}: {completed.stderr[-300:]}")
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    format_row = payload.get("format") if isinstance(payload, Mapping) else None
    if not isinstance(streams, list) or not isinstance(format_row, Mapping):
        raise HoldoutSourceFreezeError(f"ffprobe output is incomplete for {path}")
    video = [row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "video"]
    audio = [row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "audio"]
    if not video or not audio:
        raise HoldoutSourceFreezeError(f"source media lacks audio or video: {path}")
    try:
        duration_ms = round(float(format_row["duration"]) * 1000)
    except (KeyError, TypeError, ValueError) as exc:
        raise HoldoutSourceFreezeError(f"source media duration is invalid: {path}") from exc
    if duration_ms <= 0:
        raise HoldoutSourceFreezeError(f"source media duration is not positive: {path}")
    return {
        "duration_ms": duration_ms,
        "video_codec": str(video[0].get("codec_name") or ""),
        "width": int(video[0].get("width") or 0),
        "height": int(video[0].get("height") or 0),
        "audio_codec": str(audio[0].get("codec_name") or ""),
    }


def _snapshot(paths: Sequence[Path]) -> dict[str, tuple[int, int]]:
    return {
        str(path): (metadata.st_size, metadata.st_mtime_ns)
        for path in paths
        for metadata in [_regular_file(path, label="holdout source or sidecar")]
    }


def _deduplicate_listing(paths: Sequence[Path]) -> tuple[list[Path], int]:
    """Collapse identical FUSE directory entries without collapsing distinct files."""

    by_absolute_path: dict[str, Path] = {}
    for path in paths:
        absolute = str(path.absolute())
        prior = by_absolute_path.setdefault(absolute, path)
        if prior.name != path.name:
            raise HoldoutSourceFreezeError("directory listing path identity is inconsistent")
    rows = [by_absolute_path[key] for key in sorted(by_absolute_path)]
    return rows, len(paths) - len(rows)


def _normalized_target_sha256(value: object) -> str:
    normalized = str(value or "").lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise HoldoutSourceFreezeError("adapter target sha256 is malformed")
    return "sha256:" + normalized


def _segment_receipt(
    *,
    media_path: Path,
    date: str,
    role: str,
    finalized: Mapping[str, object],
    file_closed_events: Mapping[str, list[dict[str, object]]],
    probe: Callable[[Path], Mapping[str, object]],
) -> dict[str, object]:
    stem = media_path.stem
    companion_paths = {
        "flv": media_path.with_suffix(".flv"),
        "xml": media_path.with_suffix(".xml"),
        "jsonl": media_path.with_suffix(".jsonl"),
        "meta": media_path.with_suffix(".meta.json"),
    }
    media_stat = _regular_file(media_path, label="source MP4")
    companion_stats = {
        key: _regular_file(path, label=f"source {key}")
        for key, path in companion_paths.items()
    }

    adapter_key = f"{date}/{stem}.flv"
    adapter_row = finalized.get(adapter_key)
    if not isinstance(adapter_row, Mapping):
        raise HoldoutSourceFreezeError(f"adapter finalization is missing: {adapter_key}")
    expected_target_suffix = f"/Videos/{ROOM_ID}/{date}/{stem}.mp4"
    if not str(adapter_row.get("target") or "").endswith(expected_target_suffix):
        raise HoldoutSourceFreezeError(f"adapter target binding is wrong: {adapter_key}")
    if adapter_row.get("source_size") != companion_stats["flv"].st_size:
        raise HoldoutSourceFreezeError(f"adapter source size drifted: {adapter_key}")

    event_suffix = f"Videos/{ROOM_ID}/{date}/{stem}.flv"
    matches = [
        event
        for relative_path, rows in file_closed_events.items()
        if relative_path.endswith(event_suffix)
        for event in rows
    ]
    if len(matches) != 1:
        raise HoldoutSourceFreezeError(f"expected exactly one FileClosed event: {adapter_key}")
    event = matches[0]
    event_data = event.get("EventData")
    assert isinstance(event_data, Mapping)
    if event_data.get("FileSize") != companion_stats["flv"].st_size:
        raise HoldoutSourceFreezeError(f"FileClosed size drifted: {adapter_key}")

    meta_payload = json.loads(companion_paths["meta"].read_text(encoding="utf-8"))
    if not isinstance(meta_payload, Mapping) or not meta_payload.get("event_evidence"):
        raise HoldoutSourceFreezeError(f"meta event evidence is missing: {adapter_key}")

    actual_media_sha256 = _sha256(media_path)
    expected_media_sha256 = _normalized_target_sha256(adapter_row.get("target_sha256"))
    if actual_media_sha256 != expected_media_sha256:
        raise HoldoutSourceFreezeError(f"adapter target hash drifted: {adapter_key}")
    sidecars = {
        key: {
            "path": str(path),
            "size_bytes": companion_stats[key].st_size,
            "mtime_ns": companion_stats[key].st_mtime_ns,
            "sha256": _sha256(path),
        }
        for key, path in companion_paths.items()
        if key != "flv"
    }
    return {
        "session_date": date,
        "split_role": role,
        "segment_id": stem,
        "source_media": {
            "path": str(media_path),
            "size_bytes": media_stat.st_size,
            "mtime_ns": media_stat.st_mtime_ns,
            "sha256": actual_media_sha256,
            "adapter_target_sha256": expected_media_sha256,
            "ffprobe": dict(probe(media_path)),
        },
        "source_flv_evidence": {
            "path": str(companion_paths["flv"]),
            "size_bytes": companion_stats["flv"].st_size,
            "mtime_ns": companion_stats["flv"].st_mtime_ns,
            "adapter_key": adapter_key,
            "adapter_row_sha256": _canonical_sha256(dict(adapter_row)),
            "file_closed_event_id": event.get("EventId"),
            "file_closed_event_sha256": _canonical_sha256(dict(event)),
        },
        "sidecars": sidecars,
        "meta_event_evidence": meta_payload.get("event_evidence"),
        "next_state": "PRELABEL_PACKAGE_NOT_BUILT",
    }


def freeze_sources(
    *,
    recording_root: Path,
    status_path: Path,
    adapter_state_path: Path,
    webhook_journal_path: Path,
    date_roles: Mapping[str, str],
    stability_seconds: int = 15,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    mount_evidence: Mapping[str, str] | None = None,
    probe: Callable[[Path], Mapping[str, object]] = _ffprobe,
) -> dict[str, object]:
    recording_root = recording_root.resolve()
    status_path = status_path.resolve()
    adapter_state_path = adapter_state_path.resolve()
    webhook_journal_path = webhook_journal_path.resolve()
    if not recording_root.is_dir() or recording_root.is_symlink():
        raise HoldoutSourceFreezeError("recording root is missing or unsafe")
    if set(date_roles.values()) != ALLOWED_ROLES or len(date_roles) != 2:
        raise HoldoutSourceFreezeError("exactly one holdout A and one holdout B date are required")
    if isinstance(stability_seconds, bool) or not 0 <= stability_seconds <= 60:
        raise HoldoutSourceFreezeError("stability_seconds must be between 0 and 60")

    mount = dict(mount_evidence) if mount_evidence is not None else _cloudfs_mount_evidence(recording_root)
    idle_status, status_observation = _validate_idle_status(status_path, now_epoch=now())
    finalized, adapter_state_sha256 = _load_adapter_state(adapter_state_path)
    events, webhook_journal_sha256 = _load_file_closed_events(webhook_journal_path)

    media_paths: list[Path] = []
    all_paths: list[Path] = []
    counts: dict[str, int] = {}
    duplicate_listing_entries: dict[str, int] = {}
    for date in sorted(date_roles):
        date_root = recording_root / date
        if not date_root.is_dir() or date_root.is_symlink():
            raise HoldoutSourceFreezeError(f"holdout date directory is missing or unsafe: {date}")
        rows, duplicate_count = _deduplicate_listing(
            list(date_root.glob(f"{ROOM_ID}_*.mp4"))
        )
        if not rows:
            raise HoldoutSourceFreezeError(f"holdout date has no source MP4: {date}")
        counts[date] = len(rows)
        duplicate_listing_entries[date] = duplicate_count
        media_paths.extend(rows)
        for media_path in rows:
            all_paths.extend(
                [
                    media_path,
                    media_path.with_suffix(".flv"),
                    media_path.with_suffix(".xml"),
                    media_path.with_suffix(".jsonl"),
                    media_path.with_suffix(".meta.json"),
                ]
            )
    first_snapshot = _snapshot(all_paths)
    sleep(stability_seconds)
    second_snapshot = _snapshot(all_paths)
    if first_snapshot != second_snapshot:
        raise HoldoutSourceFreezeError("source inventory changed during stability observation")

    segments = [
        _segment_receipt(
            media_path=path,
            date=path.parent.name,
            role=date_roles[path.parent.name],
            finalized=finalized,
            file_closed_events=events,
            probe=probe,
        )
        for path in media_paths
    ]
    if _snapshot(all_paths) != second_snapshot:
        raise HoldoutSourceFreezeError("source inventory changed during hashing")

    deterministic_payload = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "status": "SOURCE_FROZEN",
        "room_id": ROOM_ID,
        "recording_root": str(recording_root),
        "mount": mount,
        "recording_idle_authority": idle_status,
        "date_roles": dict(sorted(date_roles.items())),
        "source_inventory_count_by_date": dict(sorted(counts.items())),
        "source_inventory_total": len(segments),
        "source_inventory_stat_sha256": _canonical_sha256(second_snapshot),
        "segments": segments,
        "builder_sha256": _sha256(Path(__file__).resolve()),
        "authority": {
            "asr_frozen": False,
            "cue_table_frozen": False,
            "predictions_frozen": False,
            "human_truth_opened": False,
            "production_authority": False,
            "deployment_authority": False,
            "next_required_state": "PRELABEL_PACKAGE_FROZEN",
        },
    }
    return {
        **deterministic_payload,
        "deterministic_payload_sha256": _canonical_sha256(deterministic_payload),
        "observation": {
            "observed_at": dt.datetime.fromtimestamp(now(), tz=dt.UTC).isoformat(),
            **status_observation,
            "adapter_state_path": str(adapter_state_path),
            "adapter_state_sha256": adapter_state_sha256,
            "webhook_journal_path": str(webhook_journal_path),
            "webhook_journal_sha256": webhook_journal_sha256,
            "stability_seconds": stability_seconds,
            "duplicate_fuse_listing_entries_by_date": duplicate_listing_entries,
        },
    }


def _parse_date_role(value: str) -> tuple[str, str]:
    try:
        date, role = value.split("=", 1)
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date role must be YYYY-MM-DD=ROLE") from exc
    if role not in ALLOWED_ROLES:
        raise argparse.ArgumentTypeError(f"unsupported holdout source role: {role}")
    return date, role


def _write_create_only(path: Path, payload: Mapping[str, object]) -> None:
    path = path.resolve()
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise HoldoutSourceFreezeError("output parent must already be a regular directory")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise HoldoutSourceFreezeError(f"output already exists (create-only): {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording-root", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--adapter-state", type=Path, required=True)
    parser.add_argument("--webhook-journal", type=Path, required=True)
    parser.add_argument("--date-role", action="append", type=_parse_date_role, required=True)
    parser.add_argument("--stability-seconds", type=int, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    date_roles = dict(args.date_role)
    if len(date_roles) != len(args.date_role):
        raise HoldoutSourceFreezeError("holdout dates must not be duplicated")
    payload = freeze_sources(
        recording_root=args.recording_root,
        status_path=args.status,
        adapter_state_path=args.adapter_state,
        webhook_journal_path=args.webhook_journal,
        date_roles=date_roles,
        stability_seconds=args.stability_seconds,
    )
    _write_create_only(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
