"""Fail-closed preparation for reviewed-subtitle baseline package replays.

This is deliberately a Talk-only correction lane.  It can prove that an old
record's media bytes can be reconstructed from the sealed padded source and it
can replay the repository-sealed v2 reviewed baseline into a private stage.
It cannot turn that partial stage into a public package: speaker, burn,
title/cover, final-review and package-audit surfaces must all be rebuilt and
sealed before a separate state-last delivery transaction may install anything.

Keeping that distinction explicit prevents the historic failure mode where a
failed rerun overwrote ``*.recut.mp4`` before its record after-image existed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
)
from src.autoslice.recut_materialization import (
    _fresh_srt_to_source_cues,
    _write_source_range_srt,
)
from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaseline,
    ReviewedSubtitleBaselineRegistryError,
    load_candidate_reviewed_subtitle_baseline,
)


REPLAY_STAGE_SCHEMA = "reviewed-baseline-replay-stage.v1"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_PADDED = re.compile(r"padded_(\d+)_(\d+)\.mp4\Z")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReviewedBaselineReplayError(RuntimeError):
    """A reviewed baseline cannot safely be prepared for package replay."""


@dataclass(frozen=True, slots=True)
class RegularBinding:
    path: Path
    sha256: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    date: str
    candidate_id: str
    package_root: Path
    record_path: Path
    padded_path: Path
    local_start_ms: int
    local_end_ms: int
    expected_video_sha256: str
    baseline: ReviewedSubtitleBaseline
    matrix: tuple[dict[str, str], ...]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_directory(path: Path) -> Path:
    absolute = Path(path).absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        try:
            observed = os.lstat(cursor)
        except OSError as exc:
            raise ReviewedBaselineReplayError("REPLAY_PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise ReviewedBaselineReplayError("REPLAY_PATH_UNSAFE")
    return absolute


def regular_binding(path: Path, *, label: str) -> RegularBinding:
    """Stream one regular file without retaining media bytes in memory."""

    path = Path(path)
    _safe_directory(path.parent)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNAVAILABLE") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (
            before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode),
            before.st_size, before.st_mtime_ns, before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode),
                opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != identity
        ):
            raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT") from exc
    observed = (
        after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode),
        after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_ctime_ns,
    )
    named = (
        after_path.st_dev, after_path.st_ino, stat.S_IMODE(after_path.st_mode),
        after_path.st_size, after_path.st_mtime_ns, after_path.st_ctime_ns,
    )
    if observed != identity or named != identity or stat.S_ISLNK(after_path.st_mode):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
    return RegularBinding(path, "sha256:" + digest.hexdigest(), *identity)


def _load_json(binding: RegularBinding, *, label: str) -> dict[str, Any]:
    payload = _read_small_bytes(binding, label=label)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_INVALID")
    return value


def _read_small_bytes(binding: RegularBinding, *, label: str) -> bytes:
    """Return a bounded control file only when its stable binding still holds."""

    if binding.size > 4 * 1024 * 1024:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(binding.path, flags)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns,
        ) != (
            binding.device, binding.inode, binding.mode, binding.size,
            binding.mtime_ns, binding.ctime_ns,
        ):
            raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if _sha(payload) != binding.sha256 or regular_binding(binding.path, label=label) != binding:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
    return payload


def _baseline(*, repo_root: Path, candidate_id: str) -> ReviewedSubtitleBaseline:
    root = repo_root / "assets" / "lidousha" / "reviewed_subtitle_baselines"
    try:
        value = load_candidate_reviewed_subtitle_baseline(
            root, candidate_id, repo_root=repo_root
        )
    except ReviewedSubtitleBaselineRegistryError as exc:
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_INVALID") from exc
    if value is None:
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_MISSING")
    config = value.config
    ownership = config.get("operator_text_full_ownership")
    lanes = config.get("operator_truth_lanes")
    if (
        config.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or config.get("exact_interval_replay") is not True
        or not isinstance(ownership, Mapping)
        or ownership.get("schema_version")
        != "operator-reviewed-text-full-ownership-pin.v3"
        or not isinstance(lanes, Mapping)
        or not isinstance(lanes.get("decision_ledger"), Mapping)
    ):
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_AUTHORITY_INCOMPLETE")
    return value


def _padded_from_provenance(recut_root: Path) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(recut_root.glob("*.recut.provenance.json"))
    if len(candidates) != 1:
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_AMBIGUOUS")
    provenance = _load_json(regular_binding(candidates[0], label="PROVENANCE"), label="PROVENANCE")
    final = provenance.get("final_recut")
    if not isinstance(final, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_INVALID")
    raw = final.get("source_path")
    if not isinstance(raw, str):
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_INVALID")
    source = Path(raw)
    if source.parent != recut_root.parent:
        raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_ESCAPES_CANDIDATE")
    return source, dict(final)


def build_replay_plan(*, repo_root: Path, out_root: Path, date: str, candidate_id: str) -> ReplayPlan:
    """Bind one current failed package to its sealed reviewed baseline.

    This is read-only.  The returned matrix intentionally records the remaining
    final-package work rather than claiming a reviewed SRT is a release.
    """

    if not _DATE.fullmatch(date) or not _CID.fullmatch(candidate_id):
        raise ReviewedBaselineReplayError("REPLAY_IDENTITY_INVALID")
    repo_root = _safe_directory(repo_root)
    package_root = _safe_directory(out_root / date / candidate_id)
    recut_root = _safe_directory(package_root / "replacement_recuts")
    record_path = recut_root / f"{candidate_id}.record.json"
    record = _load_json(regular_binding(record_path, label="RECORD"), label="RECORD")
    artifacts = record.get("artifact_hashes")
    expected = artifacts.get("video_sha256") if isinstance(artifacts, Mapping) else None
    if not isinstance(expected, str) or _SHA.fullmatch(expected) is None:
        raise ReviewedBaselineReplayError("REPLAY_RECORD_VIDEO_BINDING_INVALID")
    baseline = _baseline(repo_root=repo_root, candidate_id=candidate_id)
    config = baseline.config
    padded, final = _padded_from_provenance(recut_root)
    padded_binding = regular_binding(padded, label="PADDED_SOURCE")
    declared_padded = final.get("source_sha256")
    if declared_padded != padded_binding.sha256.removeprefix("sha256:"):
        raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_DRIFT")
    match = _PADDED.fullmatch(padded.name)
    if match is None:
        raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_NAME_INVALID")
    padded_start, padded_end = (int(value) for value in match.groups())
    boundary = record.get("boundary_audit")
    start = boundary.get("final_start_ms") if isinstance(boundary, Mapping) else None
    end = boundary.get("final_end_ms") if isinstance(boundary, Mapping) else None
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or not 0 <= start < end <= padded_end - padded_start
        or record.get("duration_ms") != end - start
    ):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_TIMING_INVALID")
    diagnostic = config.get("operator_truth_lanes", {}).get("pipeline_diagnostic")
    if not isinstance(diagnostic, Mapping) or not isinstance(diagnostic.get("path"), str):
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_MISSING")
    diagnostic_path = baseline.manifest_path.parent / str(diagnostic["path"])
    diagnostic_binding = regular_binding(diagnostic_path, label="PIPELINE_DIAGNOSTIC")
    if diagnostic_binding.sha256.removeprefix("sha256:") != diagnostic.get("sha256"):
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_DRIFT")
    matrix = (
        {"predicate": "RECORD_OLD_VIDEO_SHA256", "status": "PASS"},
        {"predicate": "PADDED_SOURCE_BINDING", "status": "PASS"},
        {"predicate": "REVIEWED_BASELINE_V2_V3_LEDGER", "status": "PASS"},
        {"predicate": "PIPELINE_DIAGNOSTIC_HASH_AND_GEOMETRY", "status": "PASS"},
        {"predicate": "SPEAKER_ASS_BURN_REBUILD", "status": "PENDING_STAGE"},
        {"predicate": "TITLE_COVER_PRECONDITIONS", "status": "PENDING_STAGE"},
        {"predicate": "FINAL_REVIEW_AND_PACKAGE_AUDIT", "status": "PENDING_STAGE"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    )
    return ReplayPlan(
        date, candidate_id, package_root, record_path, padded,
        start, end, expected, baseline, matrix,
    )


def _mkdir_private(path: Path) -> Path:
    _safe_directory(path.parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_EXISTS") from exc
    observed = os.lstat(path)
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o700:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE")
    return path


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise ReviewedBaselineReplayError("REPLAY_PRIVATE_WRITE_FAILED")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if stat.S_IMODE(os.lstat(path).st_mode) != 0o600:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE")


def stage_replay(
    plan: ReplayPlan, *, stage_parent: Path, run_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a private recut/SRT/audit stage; no live target is named writable."""

    stage_parent = _safe_directory(stage_parent)
    seed = _sha(_canonical({
        "date": plan.date, "candidate_id": plan.candidate_id,
        "record": regular_binding(plan.record_path, label="RECORD").sha256,
        "baseline": "sha256:" + plan.baseline.config["sha256"],
    })).removeprefix("sha256:")
    stage = _mkdir_private(stage_parent / f"{plan.date}-{plan.candidate_id}-{seed[:16]}")
    media = stage / "recut.mp4"
    command = list(run_command or ())
    if not command:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.000",
            "-i", str(plan.padded_path), "-ss", f"{plan.local_start_ms / 1000:.3f}",
            "-t", f"{(plan.local_end_ms - plan.local_start_ms) / 1000:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
            "-b:a", "128k", "-movflags", "+faststart", str(media),
        ]
    if command[-1] != str(media):
        raise ReviewedBaselineReplayError("REPLAY_COMMAND_TARGET_INVALID")
    completed = subprocess.run(command, check=False, capture_output=True)
    if completed.returncode != 0:
        raise ReviewedBaselineReplayError("REPLAY_ACCURATE_RECUT_FAILED")
    try:
        os.chmod(media, 0o600)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE") from exc
    rebuilt = regular_binding(media, label="STAGED_VIDEO")
    if rebuilt.sha256 != plan.expected_video_sha256:
        raise ReviewedBaselineReplayError("REPLAY_OLD_RECORD_VIDEO_SHA256_MISMATCH")
    config = plan.baseline.config
    diagnostic = config["operator_truth_lanes"]["pipeline_diagnostic"]
    diagnostic_path = plan.baseline.manifest_path.parent / str(diagnostic["path"])
    diagnostic_binding = regular_binding(diagnostic_path, label="PIPELINE_DIAGNOSTIC")
    try:
        text_value = _read_small_bytes(
            diagnostic_binding, label="PIPELINE_DIAGNOSTIC"
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_INVALID") from exc
    match = _PADDED.fullmatch(plan.padded_path.name)
    assert match is not None  # already proved while binding the plan
    padded_start = int(match.group(1))
    baseline_start = int(config["absolute_source_start_ms"])
    baseline_end = int(config["absolute_source_end_ms"])
    try:
        source_cues = _fresh_srt_to_source_cues(
            text_value, window_start_ms=baseline_start,
            duration_ms=baseline_end - baseline_start,
        )
    except ValueError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_GEOMETRY_INVALID") from exc
    current_start = padded_start + plan.local_start_ms
    current_end = padded_start + plan.local_end_ms
    automatic = stage / "automatic-window.srt"
    _write_source_range_srt(source_cues, current_start, current_end, automatic)
    automatic_binding = regular_binding(automatic, label="AUTOMATIC_WINDOW")
    try:
        automatic_text = _read_small_bytes(automatic_binding, label="AUTOMATIC_WINDOW").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewedBaselineReplayError("REPLAY_AUTOMATIC_WINDOW_INVALID") from exc
    reviewed, audit = apply_redelivery_subtitle_baseline(
        automatic_text, config=config, spec_parent=plan.baseline.manifest_path.parent,
        current_source_start_ms=current_start,
        current_source_end_ms=current_end,
        current_source_recording_basename=str(config["source_recording_basename"]),
        current_source_sha256=str(config["source_sha256"]),
    )
    if audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_APPLICATION_FAILED")
    automatic.unlink()
    _write_private(stage / "reviewed.srt", reviewed.encode("utf-8"))
    _write_private(stage / "redelivery-baseline.json", _canonical(audit))
    document: dict[str, Any] = {
        "schema_version": REPLAY_STAGE_SCHEMA,
        "date": plan.date,
        "candidate_id": plan.candidate_id,
        "record_path": str(plan.record_path),
        "record_sha256": regular_binding(plan.record_path, label="RECORD").sha256,
        "expected_video_sha256": plan.expected_video_sha256,
        "baseline_manifest": str(plan.baseline.manifest_path),
        "baseline_sha256": "sha256:" + str(plan.baseline.config["sha256"]),
        "artifacts": {
            "video": regular_binding(media, label="STAGED_VIDEO").sha256,
            "subtitle": regular_binding(stage / "reviewed.srt", label="STAGED_SRT").sha256,
            "baseline_audit": regular_binding(stage / "redelivery-baseline.json", label="STAGED_AUDIT").sha256,
        },
        "predicate_matrix": list(plan.matrix),
        "upload_allowed": False,
    }
    document["stage_sha256"] = _sha(_canonical(document))
    _write_private(stage / "stage.json", _canonical(document))
    return {"stage": str(stage), "stage_sha256": document["stage_sha256"], "predicate_matrix": document["predicate_matrix"]}
