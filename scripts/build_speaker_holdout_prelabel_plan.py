#!/usr/bin/env python3
"""Freeze a plan for holdout-only ASR extraction without executing it.

This planner is intentionally bound to the accepted 2026-08-10/11 source
freeze.  It revalidates both accepted manifests and the current source bytes,
then writes one create-only plan below the exact authorized scratch root.  It
does not create a run directory, decode audio, call an ASR provider, construct
speaker predictions, or open human truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path


SCHEMA_VERSION = "speaker-holdout-extraction-plan.v1"
SOURCE_SCHEMA_VERSION = "speaker-holdout-source-freeze.v0"
SOURCE_PURPOSE = "LOCKED_SOURCE_ONLY_NO_ASR_PREDICTION_TRUTH_OR_PRODUCTION_AUTHORITY"
PURPOSE = "HOLDOUT_PRELABEL_EXTRACTION_PLAN_ONLY_NO_TRUTH_PREDICTION_OR_PRODUCTION_AUTHORITY"
ALLOWED_SCRATCH_ROOT = Path(
    "/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v1"
)
CURRENT_ACCEPTANCE: Mapping[str, object] = {
    "acceptance_id": "lidousha-speaker-holdout-source-20260810-20260811.v0",
    "room_id": "22966160",
    "date_roles": {
        "2026-08-10": "HOLDOUT_A_SOURCE_FROZEN_UNLABELED",
        "2026-08-11": "HOLDOUT_B_SOURCE_FROZEN_UNLABELED",
    },
    "source_inventory_count_by_date": {"2026-08-10": 10, "2026-08-11": 5},
    "deterministic_payload_sha256": (
        "sha256:c4e27632a0c279747697e3168d2a91cab535967a5d186fcd3e0c5cb0ff284184"
    ),
    "replay_file_sha256": {
        "e80baec84ac8de4bca7de5a1edb265da732de2c4cf674aba29b8886c4c37de57",
        "18c71fe21a3f4ab89c0233b053a6e030308b57c8f4eb56745c7d4576c4ba7370",
    },
}
EXPECTED_SOURCE_AUTHORITY = {
    "asr_frozen": False,
    "cue_table_frozen": False,
    "predictions_frozen": False,
    "human_truth_opened": False,
    "production_authority": False,
    "deployment_authority": False,
    "next_required_state": "PRELABEL_PACKAGE_FROZEN",
}
SOURCE_TOP_LEVEL_FIELDS = {
    "schema_version",
    "purpose",
    "status",
    "room_id",
    "recording_root",
    "mount",
    "recording_idle_authority",
    "date_roles",
    "source_inventory_count_by_date",
    "source_inventory_total",
    "source_inventory_stat_sha256",
    "segments",
    "builder_sha256",
    "authority",
    "deterministic_payload_sha256",
    "observation",
}
SEGMENT_FIELDS = {
    "session_date",
    "split_role",
    "segment_id",
    "source_media",
    "source_flv_evidence",
    "sidecars",
    "meta_event_evidence",
    "next_state",
}
SOURCE_MEDIA_FIELDS = {
    "path",
    "size_bytes",
    "mtime_ns",
    "sha256",
    "adapter_target_sha256",
    "ffprobe",
}
FORBIDDEN_REMOTE_ROOTS = (
    Path("/opt/bilive/autoslice/state"),
    Path("/opt/bilive/autoslice/out"),
    Path("/opt/bilive/autoslice/repo"),
    Path("/opt/bilive/recording"),
)
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class HoldoutPrelabelPlanError(RuntimeError):
    pass


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise HoldoutPrelabelPlanError(f"{label} is missing: {absolute}") from exc
    if not stat.S_ISREG(metadata.st_mode) or absolute.is_symlink() or absolute != resolved:
        raise HoldoutPrelabelPlanError(
            f"{label} must have a regular non-symlink path chain: {absolute}"
        )
    return resolved


def _load_json(path: Path, *, label: str) -> tuple[Path, dict[str, object]]:
    path = _regular_file(path, label=label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutPrelabelPlanError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise HoldoutPrelabelPlanError(f"{label} must contain one JSON object")
    return path, value


def _is_sha256(value: object, *, prefixed: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.removeprefix("sha256:") if prefixed else value
    return (
        (not prefixed or value.startswith("sha256:"))
        and len(candidate) == 64
        and all(char in "0123456789abcdef" for char in candidate)
    )


def _deterministic_source_payload(manifest: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"deterministic_payload_sha256", "observation"}
    }


def _validate_segment(
    row: object,
    *,
    recording_root: Path,
    date_roles: Mapping[str, str],
) -> dict[str, object]:
    if not isinstance(row, dict) or set(row) != SEGMENT_FIELDS:
        raise HoldoutPrelabelPlanError("source-freeze segment field set drifted")
    session_date = row.get("session_date")
    split_role = row.get("split_role")
    segment_id = row.get("segment_id")
    if not all(isinstance(value, str) and value for value in (session_date, split_role, segment_id)):
        raise HoldoutPrelabelPlanError("source-freeze segment identity is invalid")
    if date_roles.get(session_date) != split_role:
        raise HoldoutPrelabelPlanError(f"source-freeze split role drifted: {segment_id}")
    if row.get("next_state") != "PRELABEL_PACKAGE_NOT_BUILT":
        raise HoldoutPrelabelPlanError(f"source-freeze next state drifted: {segment_id}")

    source = row.get("source_media")
    if not isinstance(source, dict) or set(source) != SOURCE_MEDIA_FIELDS:
        raise HoldoutPrelabelPlanError(f"source-media field set drifted: {segment_id}")
    source_path_raw = source.get("path")
    if not isinstance(source_path_raw, str) or not Path(source_path_raw).is_absolute():
        raise HoldoutPrelabelPlanError(f"source-media path is not absolute: {segment_id}")
    source_path = Path(source_path_raw)
    expected_parent = recording_root / session_date
    if source_path.parent != expected_parent or source_path.stem != segment_id:
        raise HoldoutPrelabelPlanError(f"source-media path identity drifted: {segment_id}")
    media_sha256 = source.get("sha256")
    if not _is_sha256(media_sha256, prefixed=True) or source.get(
        "adapter_target_sha256"
    ) != media_sha256:
        raise HoldoutPrelabelPlanError(f"source-media hash binding drifted: {segment_id}")
    size_bytes = source.get("size_bytes")
    mtime_ns = source.get("mtime_ns")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or isinstance(mtime_ns, bool)
        or not isinstance(mtime_ns, int)
    ):
        raise HoldoutPrelabelPlanError(f"source-media stat binding is invalid: {segment_id}")
    probe = source.get("ffprobe")
    if (
        not isinstance(probe, dict)
        or isinstance(probe.get("duration_ms"), bool)
        or not isinstance(probe.get("duration_ms"), int)
        or probe["duration_ms"] <= 0
        or not probe.get("audio_codec")
    ):
        raise HoldoutPrelabelPlanError(f"source-media audio binding is invalid: {segment_id}")

    return {
        "session_date": session_date,
        "split_role": split_role,
        "segment_id": segment_id,
        "source": {
            "path": source_path_raw,
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
            "sha256": media_sha256,
            "adapter_target_sha256": source["adapter_target_sha256"],
            "container_duration_ms": probe["duration_ms"],
        },
    }


def _revalidate_current_source(row: Mapping[str, object]) -> None:
    segment_id = str(row["segment_id"])
    source = row["source"]
    current_path = _regular_file(Path(str(source["path"])), label=f"source media {segment_id}")
    before = current_path.stat()
    if before.st_size != source["size_bytes"] or before.st_mtime_ns != source["mtime_ns"]:
        raise HoldoutPrelabelPlanError(f"source-media stat drifted: {segment_id}")
    if "sha256:" + _sha256(current_path) != source["sha256"]:
        raise HoldoutPrelabelPlanError(f"source-media bytes drifted: {segment_id}")
    after = current_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise HoldoutPrelabelPlanError(f"source-media changed while hashing: {segment_id}")


def _validate_source_manifest(
    manifest: Mapping[str, object],
    *,
    acceptance: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if set(manifest) != SOURCE_TOP_LEVEL_FIELDS:
        raise HoldoutPrelabelPlanError("source-freeze top-level field set drifted")
    if manifest.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise HoldoutPrelabelPlanError("source-freeze schema is not supported")
    if manifest.get("purpose") != SOURCE_PURPOSE or manifest.get("status") != "SOURCE_FROZEN":
        raise HoldoutPrelabelPlanError("source-freeze purpose or status is invalid")
    if manifest.get("room_id") != acceptance["room_id"]:
        raise HoldoutPrelabelPlanError("source-freeze room ID drifted")
    if manifest.get("authority") != EXPECTED_SOURCE_AUTHORITY:
        raise HoldoutPrelabelPlanError("source-freeze authority is not source-only")
    deterministic = _deterministic_source_payload(manifest)
    digest = manifest.get("deterministic_payload_sha256")
    if not _is_sha256(digest, prefixed=True) or digest != _canonical_sha256(deterministic):
        raise HoldoutPrelabelPlanError("source-freeze deterministic payload hash drifted")
    if digest != acceptance["deterministic_payload_sha256"]:
        raise HoldoutPrelabelPlanError("source-freeze payload is not the accepted v3 payload")

    recording_root_raw = manifest.get("recording_root")
    date_roles = manifest.get("date_roles")
    segments = manifest.get("segments")
    expected_counts = acceptance["source_inventory_count_by_date"]
    if not isinstance(recording_root_raw, str) or not Path(recording_root_raw).is_absolute():
        raise HoldoutPrelabelPlanError("source-freeze recording root is invalid")
    if date_roles != acceptance["date_roles"]:
        raise HoldoutPrelabelPlanError("source-freeze date roles drifted")
    if manifest.get("source_inventory_count_by_date") != expected_counts:
        raise HoldoutPrelabelPlanError("source-freeze accepted inventory counts drifted")
    if manifest.get("source_inventory_total") != sum(expected_counts.values()):
        raise HoldoutPrelabelPlanError("source-freeze accepted inventory total drifted")
    if not isinstance(segments, list) or not segments:
        raise HoldoutPrelabelPlanError("source-freeze segment population is empty")
    validated = [
        _validate_segment(
            row,
            recording_root=Path(recording_root_raw),
            date_roles=date_roles,
        )
        for row in segments
    ]
    counts = {
        date: sum(row["session_date"] == date for row in validated)
        for date in sorted(date_roles)
    }
    if counts != expected_counts:
        raise HoldoutPrelabelPlanError("source-freeze segment counts drifted")
    for key in ("segment_id",):
        if len({row[key] for row in validated}) != len(validated):
            raise HoldoutPrelabelPlanError(f"source-freeze contains duplicate {key}")
    for key in ("path", "sha256"):
        if len({row["source"][key] for row in validated}) != len(validated):
            raise HoldoutPrelabelPlanError(f"source-freeze contains duplicate source {key}")
    return deterministic, validated


def _validate_scratch_root(scratch_root: Path, *, allowed_root: Path) -> Path:
    if not scratch_root.is_absolute() or not allowed_root.is_absolute():
        raise HoldoutPrelabelPlanError("scratch and allowed roots must be absolute")
    try:
        metadata = scratch_root.lstat()
        resolved = scratch_root.resolve(strict=True)
    except OSError as exc:
        raise HoldoutPrelabelPlanError("scratch root must already exist") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or scratch_root.is_symlink()
        or scratch_root.absolute() != resolved
        or resolved != allowed_root.resolve(strict=True)
    ):
        raise HoldoutPrelabelPlanError("scratch root is not the exact allowed non-symlink root")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HoldoutPrelabelPlanError("scratch root must be current-owner mode 0700 or stricter")
    if any(resolved == root or resolved in root.parents or root in resolved.parents for root in FORBIDDEN_REMOTE_ROOTS):
        raise HoldoutPrelabelPlanError("scratch root overlaps a production root")
    return resolved


def _validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise HoldoutPrelabelPlanError("run ID must be a safe lowercase path component")
    return run_id


def _input_bindings(paths: Sequence[Path]) -> dict[Path, str]:
    return {path: _sha256(path) for path in paths}


def build_plan(
    *,
    source_freeze_paths: Sequence[Path],
    scratch_root: Path,
    run_id: str,
    asr_script: Path,
    acceptance: Mapping[str, object] = CURRENT_ACCEPTANCE,
    allowed_scratch_root: Path = ALLOWED_SCRATCH_ROOT,
) -> tuple[dict[str, object], Path, dict[Path, str]]:
    if len(source_freeze_paths) != 2:
        raise HoldoutPrelabelPlanError("exactly two source-freeze replays are required")
    run_id = _validate_run_id(run_id)
    scratch_root = _validate_scratch_root(scratch_root, allowed_root=allowed_scratch_root)
    asr_script = _regular_file(asr_script, label="ASR client")
    loaded = [
        _load_json(path, label=f"source-freeze replay {index}")
        for index, path in enumerate(source_freeze_paths, 1)
    ]
    replay_hashes = {_sha256(path) for path, _ in loaded}
    if replay_hashes != set(acceptance["replay_file_sha256"]):
        raise HoldoutPrelabelPlanError("source-freeze replay file hashes are not accepted v3")
    validated = [
        _validate_source_manifest(manifest, acceptance=acceptance) for _, manifest in loaded
    ]
    if validated[0][0] != validated[1][0]:
        raise HoldoutPrelabelPlanError("source-freeze replays are not deterministic-payload identical")
    deterministic_source, segments = validated[0]
    for segment in segments:
        _revalidate_current_source(segment)
    run_root = scratch_root / run_id
    if run_root.exists() or run_root.is_symlink():
        raise HoldoutPrelabelPlanError("planned run root already exists")
    output_path = scratch_root / f"{run_id}.plan.json"

    sessions = []
    for session_date in sorted(acceptance["date_roles"]):
        session_segments = []
        for row in sorted(
            (item for item in segments if item["session_date"] == session_date),
            key=lambda item: item["segment_id"],
        ):
            session_segments.append(
                {
                    **row,
                    "outputs": {
                        "attempt_root_template": (
                            f"segments/{row['segment_id']}/{{attempt_id}}"
                        ),
                        "canonical_pcm_s16le": "canonical-pcm.s16le",
                        "asr_input_mp3": "asr-input-16k-mono-64k.mp3",
                        "asr_normalized_json": "asr.normalized.json",
                        "cue_table_json": "cue-table.json",
                        "asr_srt": "asr.srt",
                        "extraction_receipt": "extraction-receipt.json",
                    },
                    "state": "PLANNED_NOT_EXECUTED",
                }
            )
        sessions.append(
            {
                "session_group_id": f"{acceptance['room_id']}:{session_date}",
                "session_date": session_date,
                "split_role": acceptance["date_roles"][session_date],
                "segments": session_segments,
            }
        )

    planner = _regular_file(Path(__file__), label="planner")
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "status": "EXTRACTION_PLAN_FROZEN_EXECUTION_NOT_AUTHORIZED",
        "source_freeze": {
            "acceptance_id": acceptance["acceptance_id"],
            "deterministic_payload_sha256": acceptance["deterministic_payload_sha256"],
            "source_inventory_stat_sha256": deterministic_source[
                "source_inventory_stat_sha256"
            ],
            "replays": sorted(
                (
                    {"path": str(path), "file_sha256": _sha256(path)}
                    for path, _ in loaded
                ),
                key=lambda row: row["file_sha256"],
            ),
        },
        "scratch": {
            "allowed_root": str(scratch_root),
            "planned_run_root": str(run_root),
            "plan_path": str(output_path),
            "write_policy": "CREATE_ONLY_NO_OVERWRITE",
        },
        "policy": {
            "session_grouping": "ROOM_AND_DATE_ONE_SESSION",
            "selection": "COMPLETE_FROZEN_SOURCE_INVENTORY_NO_SCORE_OR_TRUTH_FILTER",
            "canonical_pcm": {
                "sample_rate_hz": 16_000,
                "channels": 1,
                "sample_width_bytes": 2,
                "endianness": "little",
                "container": "raw_s16le",
            },
            "asr_provider": "bcut",
            "asr_model_id": "7",
            "threshold_state": None,
            "canonical_pcm_cross_session_dedup_required": True,
        },
        "toolchain": {
            "planner": {"path": str(planner), "sha256": f"sha256:{_sha256(planner)}"},
            "free_asr_client": {
                "path": str(asr_script),
                "sha256": f"sha256:{_sha256(asr_script)}",
            },
            "hash_bound_run_one_wrapper": None,
            "runtime_binding_state": "BLOCKED_HASH_BOUND_RUNNER_NOT_IMPLEMENTED",
        },
        "sessions": sessions,
        "session_count": len(sessions),
        "segment_count": sum(len(session["segments"]) for session in sessions),
        "execution_contract": {
            "plan_only": True,
            "real_provider_execution_authorized": False,
            "external_audio_upload_authorized": False,
            "max_segments_per_future_invocation": 1,
            "production_runner_allowed": False,
            "human_truth_allowed": False,
            "speaker_prediction_allowed": False,
            "future_source_revalidation": ["path", "size_bytes", "mtime_ns", "sha256"],
            "production_roots_forbidden": [str(path) for path in FORBIDDEN_REMOTE_ROOTS],
        },
        "authority": {
            "asr_frozen": False,
            "cue_table_frozen": False,
            "predictions_frozen": False,
            "human_truth_opened": False,
            "production_authority": False,
            "deployment_authority": False,
            "next_required_state": "IMPLEMENT_HASH_BOUND_RUN_ONE_WRAPPER",
        },
    }
    payload["deterministic_payload_sha256"] = _canonical_sha256(payload)
    bound_inputs = _input_bindings([*(path for path, _ in loaded), asr_script, planner])
    return payload, output_path, bound_inputs


def _write_create_only(
    *,
    scratch_root: Path,
    output_name: str,
    payload: Mapping[str, object],
    bound_inputs: Mapping[Path, str],
) -> Path:
    if Path(output_name).name != output_name or output_name in {"", ".", ".."}:
        raise HoldoutPrelabelPlanError("plan output name must be one path component")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(scratch_root, directory_flags)
    temporary_name = f".{output_name}.tmp-{secrets.token_hex(12)}"
    temporary_fd: int | None = None
    published = False
    try:
        root_stat = os.fstat(directory_fd)
        path_stat = scratch_root.lstat()
        if (root_stat.st_dev, root_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise HoldoutPrelabelPlanError("scratch root changed before plan commit")
        temporary_fd = os.open(temporary_name, file_flags, 0o600, dir_fd=directory_fd)
        view = memoryview(encoded)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short write while creating prelabel plan")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        for path, expected_sha256 in bound_inputs.items():
            if _sha256(_regular_file(path, label="bound plan input")) != expected_sha256:
                raise HoldoutPrelabelPlanError(f"bound input drifted before plan commit: {path}")
        try:
            os.link(
                temporary_name,
                output_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise HoldoutPrelabelPlanError(
                f"plan output already exists (create-only): {scratch_root / output_name}"
            ) from exc
        published = True
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return scratch_root / output_name
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-freeze", action="append", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--asr-script", type=Path, required=True)
    args = parser.parse_args(argv)
    payload, output_path, bound_inputs = build_plan(
        source_freeze_paths=args.source_freeze,
        scratch_root=args.scratch_root,
        run_id=args.run_id,
        asr_script=args.asr_script,
    )
    _write_create_only(
        scratch_root=args.scratch_root,
        output_name=output_path.name,
        payload=payload,
        bound_inputs=bound_inputs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
